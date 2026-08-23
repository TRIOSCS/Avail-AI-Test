"""ask_avail_service.py — Ask AVAIL: NL questions → whitelisted query templates.

Purpose (survey idea #18):
  A typed question is mapped by Claude to ONE named, parameterized query template
  and its parameters — NEVER free SQL. Each template is a hand-written ORM query
  returning a uniform (columns, rows) table the caller renders with a one-tap CSV.
  This fills the reporting-page gap without a BI page. When no template matches,
  the caller falls through to the existing entity search.

Security:
  - Claude only chooses a template NAME (validated against the registry) and fills
    a params dict; parameters are coerced per template (ints bounded, dates ISO,
    strings capped) and only ever bound through the ORM — never string-interpolated.
  - Requisition-scoped templates apply the same ownership read-gating as
    global search for RESTRICTED_ROLES (sales/trader see only requisitions they own).

Called by: routers/htmx/search_views.py (Enter-key ai_search path + ask.csv export)
Depends on: utils/claude_client, utils/claude_errors, the ORM models, constants.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import (
    RESTRICTED_ROLES,
    ActivityType,
    BuyPlanLineStatus,
    OfferStatus,
    PrepaymentStatus,
    QuoteStatus,
    RequisitionStatus,
)
from app.models.auth import User
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.intelligence import ActivityLog, MaterialCard
from app.models.offers import Offer
from app.models.quality_plan import Prepayment
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.utils.claude_client import claude_structured
from app.utils.claude_errors import ClaudeError, ClaudeUnavailableError
from app.utils.sql_helpers import escape_like
from app.vendor_utils import normalize_vendor_name

_MAX_ROWS = 100


def _contains(col, value: str):
    """A wildcard-safe case-insensitive substring filter (escapes %/_ so an AI/client-
    supplied value can't act as a LIKE metacharacter)."""
    return col.ilike(f"%{escape_like(value)}%", escape="\\")


# ── Param coercion (all AI-supplied params pass through these) ──────────────────


def _p_int(params: dict, key: str, default: int | None, *, lo: int = 0, hi: int = _MAX_ROWS) -> int | None:
    raw = params.get(key)
    if raw is None or isinstance(raw, bool):
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        return default


def _p_str(params: dict, key: str, *, max_len: int = 120) -> str | None:
    raw = params.get(key)
    if raw is None:
        return None
    text = str(raw).strip()[:max_len]
    return text or None


def _p_bool(params: dict, key: str) -> bool:
    return bool(params.get(key)) and str(params.get(key)).lower() not in ("false", "0", "no", "")


def _since(params: dict, key: str, default_days: int | None) -> datetime | None:
    """A cutoff datetime from an ISO date param or a default N-days-ago."""
    raw = _p_str(params, key, max_len=32)
    if raw:
        try:
            return datetime.combine(date.fromisoformat(raw), datetime.min.time(), tzinfo=UTC)
        except ValueError:
            pass
    if default_days is not None:
        return datetime.now(UTC) - timedelta(days=default_days)
    return None


def _owned_req_ids(db: Session, user: User | None):
    """For a RESTRICTED_ROLE user, a scalar-subquery of requisition ids they own; None
    for an unrestricted user (or legacy no-user caller) — no filtering."""
    if user is None or getattr(user, "role", None) not in RESTRICTED_ROLES:
        return None
    return select(Requisition.id).where(Requisition.created_by == user.id).scalar_subquery()


def _fmt(value: Any) -> Any:
    """Table-cell normalization: dates → ISO, everything else passthrough."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return value


# ── Templates ──────────────────────────────────────────────────────────────────
# Each run(db, user, params) -> (columns, rows). Requisition-scoped ones apply
# _owned_req_ids gating. All limited to _MAX_ROWS.


def _t_open_requisitions(db, user, params):
    q = db.query(Requisition).filter(
        Requisition.is_scratch.is_(False), Requisition.status == RequisitionStatus.OPEN.value
    )
    cust = _p_str(params, "customer")
    if cust:
        q = q.filter(_contains(Requisition.customer_name, cust))
    if _p_bool(params, "past_deadline"):
        q = q.filter(Requisition.deadline.isnot(None), Requisition.deadline < date.today().isoformat())
    older = _p_int(params, "older_than_days", None, hi=3650)
    if older:
        q = q.filter(Requisition.created_at < datetime.now(UTC) - timedelta(days=older))
    owned = _owned_req_ids(db, user)
    if owned is not None:
        q = q.filter(Requisition.id.in_(owned))
    rows = q.order_by(Requisition.created_at.desc()).limit(_MAX_ROWS).all()
    cols = ["id", "name", "customer", "deadline", "created"]
    return cols, [
        {
            "id": r.id,
            "name": r.name,
            "customer": r.customer_name,
            "deadline": _fmt(r.deadline),
            "created": _fmt(r.created_at),
        }
        for r in rows
    ]


def _t_requisitions_by_customer(db, user, params):
    q = db.query(Requisition).filter(Requisition.is_scratch.is_(False))
    cust = _p_str(params, "customer")
    if cust:
        q = q.filter(_contains(Requisition.customer_name, cust))
    since = _since(params, "since", None)
    if since:
        q = q.filter(Requisition.created_at >= since)
    until = _since(params, "until", None)
    if until:
        q = q.filter(Requisition.created_at < until + timedelta(days=1))
    owned = _owned_req_ids(db, user)
    if owned is not None:
        q = q.filter(Requisition.id.in_(owned))
    rows = q.order_by(Requisition.created_at.desc()).limit(_MAX_ROWS).all()
    cols = ["id", "name", "customer", "status", "created"]
    return cols, [
        {"id": r.id, "name": r.name, "customer": r.customer_name, "status": r.status, "created": _fmt(r.created_at)}
        for r in rows
    ]


def _t_reqs_without_quotes(db, user, params):
    older = _p_int(params, "older_than_days", 5, hi=3650)
    has_quote = select(Quote.requisition_id).where(Quote.requisition_id == Requisition.id).exists()
    q = db.query(Requisition).filter(
        Requisition.is_scratch.is_(False),
        Requisition.status == RequisitionStatus.OPEN.value,
        Requisition.created_at < datetime.now(UTC) - timedelta(days=older or 0),
        ~has_quote,
    )
    owned = _owned_req_ids(db, user)
    if owned is not None:
        q = q.filter(Requisition.id.in_(owned))
    rows = q.order_by(Requisition.created_at.asc()).limit(_MAX_ROWS).all()
    cols = ["id", "name", "customer", "created"]
    return cols, [
        {"id": r.id, "name": r.name, "customer": r.customer_name, "created": _fmt(r.created_at)} for r in rows
    ]


def _t_unanswered_quotes(db, user, params):
    older = _p_int(params, "older_than_days", 7, hi=3650)
    q = db.query(Quote).filter(
        Quote.status == QuoteStatus.SENT.value,
        Quote.sent_at.isnot(None),
        Quote.sent_at < datetime.now(UTC) - timedelta(days=older or 0),
    )
    cust = _p_str(params, "customer")
    if cust:
        q = q.join(Requisition, Quote.requisition_id == Requisition.id).filter(
            _contains(Requisition.customer_name, cust)
        )
    owned = _owned_req_ids(db, user)
    if owned is not None:
        q = q.filter(Quote.requisition_id.in_(owned))
    rows = q.order_by(Quote.sent_at.asc()).limit(_MAX_ROWS).all()
    cols = ["quote", "requisition_id", "status", "sent"]
    eff = {"older_than_days": older, **({"customer": cust} if cust else {})}
    return (
        cols,
        [
            {"quote": r.quote_number, "requisition_id": r.requisition_id, "status": r.status, "sent": _fmt(r.sent_at)}
            for r in rows
        ],
        eff,
    )


def _t_quotes_by_customer(db, user, params):
    q = db.query(Quote).join(Requisition, Quote.requisition_id == Requisition.id)
    cust = _p_str(params, "customer")
    if cust:
        q = q.filter(_contains(Requisition.customer_name, cust))
    since = _since(params, "since", None)
    if since:
        q = q.filter(Quote.created_at >= since)
    until = _since(params, "until", None)
    if until:
        q = q.filter(Quote.created_at < until + timedelta(days=1))
    owned = _owned_req_ids(db, user)
    if owned is not None:
        q = q.filter(Quote.requisition_id.in_(owned))
    rows = q.order_by(Quote.created_at.desc()).limit(_MAX_ROWS).all()
    cols = ["quote", "customer", "status", "created"]
    return cols, [
        {
            "quote": r.quote_number,
            "customer": r.requisition.customer_name if r.requisition else None,
            "status": r.status,
            "created": _fmt(r.created_at),
        }
        for r in rows
    ]


def _t_stale_pending_offers(db, user, params):
    older = _p_int(params, "older_than_days", 3, hi=3650)
    q = db.query(Offer).filter(
        Offer.status == OfferStatus.PENDING_REVIEW.value,
        Offer.created_at < datetime.now(UTC) - timedelta(days=older or 0),
    )
    owned = _owned_req_ids(db, user)
    if owned is not None:
        q = q.filter(Offer.requisition_id.in_(owned))
    rows = q.order_by(Offer.created_at.asc()).limit(_MAX_ROWS).all()
    cols = ["id", "mpn", "vendor", "requisition_id", "created"]
    return cols, [
        {
            "id": r.id,
            "mpn": r.mpn,
            "vendor": r.vendor_name,
            "requisition_id": r.requisition_id,
            "created": _fmt(r.created_at),
        }
        for r in rows
    ]


def _t_vendor_offer_history(db, user, params):
    q = db.query(Offer)
    vendor = _p_str(params, "vendor")
    if vendor:
        q = q.filter(Offer.vendor_name_normalized == normalize_vendor_name(vendor))
    since = _since(params, "since", None)
    if since:
        q = q.filter(Offer.created_at >= since)
    owned = _owned_req_ids(db, user)
    if owned is not None:
        q = q.filter(Offer.requisition_id.in_(owned))
    rows = q.order_by(Offer.created_at.desc()).limit(_MAX_ROWS).all()
    cols = ["id", "vendor", "mpn", "unit_price", "status", "created"]
    return cols, [
        {
            "id": r.id,
            "vendor": r.vendor_name,
            "mpn": r.mpn,
            "unit_price": _fmt(r.unit_price),
            "status": r.status,
            "created": _fmt(r.created_at),
        }
        for r in rows
    ]


def _lines_by_status(db, user, params, status):
    q = db.query(BuyPlanLine).filter(BuyPlanLine.status == status)
    buyer = _p_str(params, "buyer")
    if buyer:
        q = q.join(User, BuyPlanLine.buyer_id == User.id).filter(_contains(User.name, buyer))
    older = _p_int(params, "older_than_days", None, hi=3650)
    if older:
        q = q.filter(BuyPlanLine.created_at < datetime.now(UTC) - timedelta(days=older))
    owned = _owned_req_ids(db, user)
    if owned is not None:
        q = q.join(BuyPlan, BuyPlanLine.buy_plan_id == BuyPlan.id).filter(BuyPlan.requisition_id.in_(owned))
    rows = q.order_by(BuyPlanLine.created_at.asc()).limit(_MAX_ROWS).all()
    cols = ["line_id", "buy_plan_id", "po_number", "status", "created"]
    return cols, [
        {
            "line_id": r.id,
            "buy_plan_id": r.buy_plan_id,
            "po_number": r.po_number,
            "status": r.status,
            "created": _fmt(r.created_at),
        }
        for r in rows
    ]


def _t_lines_awaiting_po(db, user, params):
    return _lines_by_status(db, user, params, BuyPlanLineStatus.AWAITING_PO.value)


def _t_lines_pending_verify(db, user, params):
    return _lines_by_status(db, user, params, BuyPlanLineStatus.PENDING_VERIFY.value)


def _t_prepayments_outstanding(db, user, params):
    q = db.query(Prepayment).filter(
        Prepayment.status.in_([PrepaymentStatus.REQUESTED.value, PrepaymentStatus.APPROVED.value]),
        Prepayment.paid_at.is_(None),
        Prepayment.voided_at.is_(None),
    )
    owned = _owned_req_ids(db, user)
    if owned is not None:
        q = q.join(BuyPlan, Prepayment.buy_plan_id == BuyPlan.id).filter(BuyPlan.requisition_id.in_(owned))
    rows = q.order_by(Prepayment.created_at.asc()).limit(_MAX_ROWS).all()
    cols = ["id", "vendor", "amount", "status", "created"]
    return cols, [
        {
            "id": r.id,
            "vendor": r.vendor_name,
            "amount": _fmt(r.total_incl_fees),
            "status": r.status,
            "created": _fmt(r.created_at),
        }
        for r in rows
    ]


def _t_top_searched_parts(db, user, params):
    limit = _p_int(params, "limit", 20) or 20
    rows = (
        db.query(MaterialCard)
        .filter(MaterialCard.deleted_at.is_(None), MaterialCard.search_count > 0)
        .order_by(MaterialCard.search_count.desc())
        .limit(limit)
        .all()
    )
    cols = ["mpn", "manufacturer", "search_count", "last_searched"]
    return cols, [
        {
            "mpn": r.display_mpn,
            "manufacturer": r.manufacturer,
            "search_count": r.search_count,
            "last_searched": _fmt(r.last_searched_at),
        }
        for r in rows
    ]


def _t_user_edits(db, user, params):
    q = db.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.FIELD_EDIT.value)
    # A restricted user may only inspect their OWN edit history; a manager/admin can
    # name any user (matched on User.name).
    if user is not None and getattr(user, "role", None) in RESTRICTED_ROLES:
        q = q.filter(ActivityLog.user_id == user.id)
    else:
        name = _p_str(params, "user_name")
        if name:
            q = q.join(User, ActivityLog.user_id == User.id).filter(_contains(User.name, name))
    since = _since(params, "since", 7)
    if since:
        q = q.filter(ActivityLog.created_at >= since)
    rows = q.order_by(ActivityLog.created_at.desc()).limit(_MAX_ROWS).all()
    cols = ["at", "user_id", "buy_plan_id", "edits"]
    return cols, [
        {
            "at": _fmt(r.created_at),
            "user_id": r.user_id,
            "buy_plan_id": r.buy_plan_id,
            "edits": len((r.details or {}).get("edits", [])),
        }
        for r in rows
    ]


def _t_approval_cycle_time(db, user, params):
    """Per-request submit→first-decision hours for buy-plan approvals."""
    from app.constants import ApprovalGateType, ApprovalSubjectType
    from app.models.approvals import ApprovalEvent, ApprovalRequest

    since = _since(params, "since", None)
    q = db.query(ApprovalRequest).filter(ApprovalRequest.gate_type == ApprovalGateType.BUY_PLAN.value)
    if since:
        q = q.filter(ApprovalRequest.created_at >= since)
    # Read-gate for restricted roles through subject→buy plan→requisition ownership
    # (owner_id alone is wrong — a manager may submit on behalf of the req creator).
    owned = _owned_req_ids(db, user)
    if owned is not None:
        owned_bp = select(BuyPlan.id).where(BuyPlan.requisition_id.in_(owned)).scalar_subquery()
        q = q.filter(
            ApprovalRequest.subject_type == ApprovalSubjectType.BUY_PLAN.value,
            ApprovalRequest.subject_id.in_(owned_bp),
        )
    reqs = q.order_by(ApprovalRequest.created_at.desc()).limit(_MAX_ROWS).all()
    cols = ["request_id", "status", "submitted", "cycle_hours"]
    out = []
    for r in reqs:
        decided = (
            db.query(ApprovalEvent.created_at)
            .filter(ApprovalEvent.request_id == r.id, ApprovalEvent.event_type.in_(["approved", "rejected"]))
            .order_by(ApprovalEvent.created_at.asc())
            .first()
        )
        hours = None
        if decided and r.created_at:
            hours = round((decided[0] - r.created_at).total_seconds() / 3600, 1)
        out.append({"request_id": r.id, "status": r.status, "submitted": _fmt(r.created_at), "cycle_hours": hours})
    return cols, out


# name → {label (human), fn}. The label doubles as the Claude enum description.
TEMPLATES: dict[str, dict[str, Any]] = {
    "open_requisitions": {
        "label": "Open requisitions",
        "fn": _t_open_requisitions,
        "params": "customer?, past_deadline?, older_than_days?",
    },
    "requisitions_by_customer": {
        "label": "Requisitions for a customer",
        "fn": _t_requisitions_by_customer,
        "params": "customer, since?, until?",
    },
    "reqs_without_quotes": {
        "label": "Open requisitions with no quote",
        "fn": _t_reqs_without_quotes,
        "params": "older_than_days?",
    },
    "unanswered_quotes": {
        "label": "Quotes sent but unanswered",
        "fn": _t_unanswered_quotes,
        "params": "older_than_days?, customer?",
    },
    "quotes_by_customer": {
        "label": "Quotes for a customer",
        "fn": _t_quotes_by_customer,
        "params": "customer, since?, until?",
    },
    "stale_pending_offers": {
        "label": "Offers stuck in pending review",
        "fn": _t_stale_pending_offers,
        "params": "older_than_days?",
    },
    "vendor_offer_history": {
        "label": "Offer history for a vendor",
        "fn": _t_vendor_offer_history,
        "params": "vendor, since?",
    },
    "lines_awaiting_po": {
        "label": "Buy-plan lines awaiting PO",
        "fn": _t_lines_awaiting_po,
        "params": "buyer?, older_than_days?",
    },
    "lines_pending_verify": {
        "label": "Buy-plan lines pending approval",
        "fn": _t_lines_pending_verify,
        "params": "older_than_days?",
    },
    "prepayments_outstanding": {
        "label": "Prepayments not yet paid",
        "fn": _t_prepayments_outstanding,
        "params": "(none)",
    },
    "top_searched_parts": {"label": "Most-searched parts", "fn": _t_top_searched_parts, "params": "limit?"},
    "user_edits": {"label": "Recent field edits by a user", "fn": _t_user_edits, "params": "user_name, since?"},
    "approval_cycle_time": {"label": "Buy-plan approval cycle time", "fn": _t_approval_cycle_time, "params": "since?"},
}

ASK_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "template": {
            "type": "string",
            "enum": [*TEMPLATES.keys(), "none"],
            "description": "The report template that answers the question, or 'none' if none fits.",
        },
        "params": {"type": "object", "description": "Parameter values for the chosen template (may be empty)."},
    },
    "required": ["template"],
}


def _intent_system() -> str:
    lines = "\n".join(f"- {name}: {t['label']} (params: {t['params']})" for name, t in TEMPLATES.items())
    return (
        "You route a user's question to ONE reporting template, or 'none' if no template fits.\n"
        "Return the template name and a params object. Only use parameters the template lists.\n"
        "Dates are ISO YYYY-MM-DD. Never guess a template just to answer — 'none' is correct when nothing fits.\n\n"
        f"Templates:\n{lines}"
    )


def run_template(db: Session, user: User | None, template: str, params: dict) -> tuple[list[str], list[dict], dict]:
    """Run a named template with coerced params → (columns, rows, effective_params).

    A template fn may return (cols, rows) or (cols, rows, effective) — the third element
    is the params it ACTUALLY applied (defaults filled in) so the UI can be honest about
    e.g. a silent 7-day window. Raises KeyError for an unknown name (callers validate
    against TEMPLATES first).
    """
    entry = TEMPLATES[template]
    result = entry["fn"](db, user, params or {})
    if len(result) == 3:
        cols, rows, effective = result
    else:
        cols, rows = result
        effective = dict(params or {})
    return cols, rows, effective


async def answer_question(db: Session, user: User | None, question: str) -> dict:
    """Map *question* to a template and run it.

    Returns {"matched": True, "template", "label", "params", "columns", "rows"} on a
    hit, else {"matched": False}. Never raises for a bad AI response — an unknown
    template name or a failed call falls through to matched=False so the caller can
    render the ordinary entity search.
    """
    q = (question or "").strip()
    if not q:
        return {"matched": False}
    try:
        intent = await claude_structured(
            q,
            ASK_INTENT_SCHEMA,
            system=_intent_system(),
            model_tier="fast",
            timeout=15,
            max_attempts=1,
            cost_bucket="ask_avail",
        )
    except (ClaudeUnavailableError, ClaudeError) as e:
        logger.info("Ask AVAIL intent parse unavailable/failed: {}", e)
        return {"matched": False}
    if not intent:
        return {"matched": False}
    template = str(intent.get("template") or "").strip()
    if template not in TEMPLATES:
        return {"matched": False}
    params = intent.get("params") if isinstance(intent.get("params"), dict) else {}
    try:
        cols, rows, effective = run_template(db, user, template, params)
    except Exception:  # noqa: BLE001 — a template failure must fall through to entity search, never 500 the search box
        logger.exception("Ask AVAIL template {} failed", template)
        return {"matched": False}
    return {
        "matched": True,
        "template": template,
        "label": TEMPLATES[template]["label"],
        "params": effective,  # what actually ran (defaults filled in), not the raw AI dict
        "columns": cols,
        "rows": rows,
    }
