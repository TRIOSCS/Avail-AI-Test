"""Per-salesperson proactive outreach digest — generate → human review → manual send.

Builds one ProactiveDigest draft per salesperson from their active (NEW)
proactive matches, in the exact copy format of the 2026-08-06 brief: grouped
by customer account (plain name line over a bulleted list, never a table),
requirements with no customer account under a "Trio Back Order" group routed
to the requisition owner, quantities with thousands separators, dates
M/D/YYYY, prices bare when whole / two decimals otherwise. Price-anchor
sub-lines (last quote / last win) are omitted entirely when there is no
record — no N/A, no placeholders — and can carry another customer's pricing,
so every digest is marked internal reference, not for forwarding.

Each line freezes a ProactiveOutreachLine snapshot (what the salesperson was
told) that later carries the contact tracking (contacted / outcome /
produced requisition / quote / sales_order_number). Regenerating replaces an
unsent draft; sent digests are permanent. NOTHING sends automatically —
send_digest is called by a human from the review pane and mails the
salesperson with the ACTOR's delegated Graph token.

A suspected-duplicate-materials section (spelling variants that collide when
spaces/hyphens/case are stripped) is appended to every affected digest —
detection only, merging is a human action in the system of record.

Called by: routers/htmx/proactive.py (generate/review/send endpoints),
    app/jobs/offers_jobs.py (weekly draft generation)
Depends on: models, services/proactive_matching, services/pricing_history
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import escape

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..constants import ProactiveDigestStatus, ProactiveMatchStatus
from ..models import (
    Company,
    Offer,
    ProactiveDigest,
    ProactiveMatch,
    ProactiveOutreachLine,
    Requirement,
    User,
)
from .pricing_history import last_quote_for_part, last_win_for_part
from .proactive_matching import (
    _requirement_window_start,
    compute_offer_rollup,
)

DIGEST_SUBJECT = "Availability match on your requirements, please check with customers"

# ── Formatting (brief-exact) ─────────────────────────────────────────────


def fmt_qty(qty: int | None) -> str:
    """Thousands separators: 1805521 → '1,805,521'."""
    return f"{qty:,}" if qty is not None else ""


def fmt_price(price: float | Decimal | None) -> str:
    """Bare when whole, two decimals otherwise: 274.0 → '274', 6.44 → '6.44'."""
    if price is None:
        return ""
    value = float(price)
    if value == int(value):
        return str(int(value))
    return f"{value:,.2f}"


def fmt_date(dt: datetime | None) -> str:
    """M/D/YYYY without leading zeros: 2026-08-03 → '8/3/2026'."""
    if not dt:
        return ""
    return f"{dt.month}/{dt.day}/{dt.year}"


def _dup_key(part: str) -> str:
    """Duplicate-detection normalization ONLY (never used for matching):

    strip spaces + hyphens + case.
    """
    return part.replace(" ", "").replace("-", "").lower()


# ── Duplicate-material detection (Step 7) ────────────────────────────────


def find_duplicate_material_groups(db: Session) -> list[list[str]]:
    """Spelling variants across in-window requirements ∪ live offers.

    Groups of distinct raw part strings that collide once spaces/hyphens/case are
    stripped — flagged for a human to merge at source, never merged here.
    """
    seen: dict[str, set[str]] = {}
    req_rows = db.scalars(
        select(Requirement.primary_mpn)
        .where(
            Requirement.primary_mpn.isnot(None),
            Requirement.created_at >= _requirement_window_start(),
        )
        .distinct()
    ).all()
    offer_rows = db.scalars(select(Offer.mpn).where(Offer.mpn.isnot(None)).distinct()).all()
    for raw in [*req_rows, *offer_rows]:
        part = (raw or "").strip()
        if part:
            seen.setdefault(_dup_key(part), set()).add(part.upper())
    return sorted(
        [sorted(variants) for variants in seen.values() if len(variants) > 1],
        key=lambda group: group[0],
    )


# ── Line + body rendering ────────────────────────────────────────────────


def _render_line(line: dict) -> list[str]:
    """One digest bullet + its indented anchor sub-lines (text form).

    Hotlist matches keep the hotlist framing even when they carry real ask history
    (part-level hotlist, migration 210) — the salesperson should read "standing
    monitor", not "recent ask".
    """
    is_hotlist = line.get("match_source") == "hotlist"
    ask = ""
    if line["last_asked_at"]:
        ask = f"last asked {fmt_date(line['last_asked_at'])} for {fmt_qty(line['last_asked_qty'])} pcs"
        if (line["requirement_count"] or 0) > 1:
            ask += f" ({line['requirement_count']} requests on file)"
        if is_hotlist:
            ask = f"on your hotlist — {ask}"
    else:
        # No ask history = requisition-level hotlist (the only source that
        # seeds without a requirement).
        ask = "on your hotlist"
    avail = f"available {fmt_qty(line['available_qty'])} pcs"
    if line["low_cost"] is not None:
        avail += f", low cost ${fmt_price(line['low_cost'])}"
    out = [f"  - {line['mpn']} | {ask} | {avail}"]

    quote = line.get("quote_anchor")
    if quote and quote["price"]:
        if line["company_id"] and quote["company_id"] == line["company_id"]:
            out.append(f"      we quoted them ${fmt_price(quote['price'])} on {fmt_date(quote['at'])}")
        else:
            to_part = f" to {quote['company']}" if quote["company"] else ""
            rep_part = f", {quote['rep']}" if quote["rep"] else ""
            out.append(
                f"      we quoted this part ${fmt_price(quote['price'])} on {fmt_date(quote['at'])}{to_part}{rep_part}"
            )
    win = line.get("win_anchor")
    if win and win["price"]:
        rep_part = f", {win['rep']}" if win["rep"] else ""
        at_part = f" at {win['company']}" if win["company"] else ""
        out.append(f"      won ${fmt_price(win['price'])} on {fmt_date(win['at'])}{rep_part}{at_part}")
    for spelling, v in (line.get("variants") or {}).items():
        if v.get("kind") == "ai":
            out.append(f"      includes {fmt_qty(v['qty'])} pcs under {spelling} — AI variant match, VERIFY")
        else:
            out.append(f"      includes {fmt_qty(v['qty'])} pcs under {spelling} (formatting variant)")
    return out


def _render_body(salesperson: User, groups: list[tuple[str, list[dict]]], dup_groups: list[list[str]]) -> str:
    """Full digest body as simple internal HTML (pre-wrapped plain text layout)."""
    first_name = (salesperson.name or salesperson.email).split()[0]
    lines: list[str] = [
        f"Hi {first_name},",
        "",
        "We matched this week's stock availabilities against requirements on file. The",
        "parts below came back with supply. Please contact each customer and find out",
        "whether they are still looking, looking again, or need more.",
        "",
    ]
    for customer_name, group_lines in groups:
        lines.append(customer_name)
        for line in group_lines:
            lines.extend(_render_line(line))
        lines.append("")
    if dup_groups:
        lines.append("Suspected duplicate materials — verify and merge at source, matched separately above:")
        for group in dup_groups:
            lines.append(f"  - {' / '.join(group)}")
        lines.append("")
    lines.append("Let me know what comes back.")
    lines.append("")
    lines.append("Internal reference — not for forwarding.")
    body_text = "\n".join(lines)
    return f'<div style="font-family: Consolas, Menlo, monospace; white-space: pre-wrap;">{escape(body_text)}</div>'


# ── Generation ───────────────────────────────────────────────────────────


def generate_digests(db: Session) -> dict:
    """Build/replace one DRAFT digest per salesperson from their NEW matches.

    Recomputes rollups + price anchors fresh at generation time (a 3-week-old match
    whose supply evaporated is skipped, not shown stale). Unsent drafts are replaced
    wholesale; SENT digests are never touched. Caller commits.
    """
    matches = (
        db.scalars(
            select(ProactiveMatch)
            .where(
                ProactiveMatch.status == ProactiveMatchStatus.NEW,
                ProactiveMatch.salesperson_id.isnot(None),
            )
            .order_by(ProactiveMatch.salesperson_id, ProactiveMatch.company_id, ProactiveMatch.mpn)
        )
        .unique()
        .all()
    )
    company_names: dict[int, str] = {}
    company_ids = {m.company_id for m in matches if m.company_id}
    if company_ids:
        company_names = dict(db.execute(select(Company.id, Company.name).where(Company.id.in_(company_ids))).all())

    dup_groups = find_duplicate_material_groups(db)

    # Drafts are disposable — clear ALL of them up front, not per-salesperson,
    # so a rep whose matches evaporated (or were reassigned) since last
    # generation doesn't keep a stale draft forever. SENT digests are untouched.
    for stale in db.scalars(select(ProactiveDigest).where(ProactiveDigest.status == ProactiveDigestStatus.DRAFT)).all():
        db.delete(stale)
    db.flush()

    # Assemble per salesperson → per customer (Trio Back Order last).
    per_sales: dict[int, dict] = {}
    for m in matches:
        rollup = compute_offer_rollup(db, part=m.mpn)
        if not rollup["offer_count"]:
            continue  # supply evaporated since the match was created
        line = {
            "match_id": m.id,
            "mpn": m.mpn,
            "company_id": m.company_id,
            "match_source": m.match_source or "requirement",
            "last_asked_at": m.last_asked_at,
            "last_asked_qty": m.last_asked_qty,
            "requirement_count": m.requirement_count or 0,
            "available_qty": rollup["available_qty"],
            "low_cost": rollup["low_cost"],
            "variants": rollup.get("variants", {}),
            "quote_anchor": last_quote_for_part(db, part=m.mpn),
            "win_anchor": last_win_for_part(db, part=m.mpn),
        }
        bucket = per_sales.setdefault(m.salesperson_id, {"customers": {}, "backorder": []})
        if m.company_id:
            customer = company_names.get(m.company_id, "Unknown account")
            bucket["customers"].setdefault(customer, []).append(line)
        else:
            bucket["backorder"].append(line)

    generated = 0
    for salesperson_id, bucket in per_sales.items():
        salesperson = db.get(User, salesperson_id)
        if not salesperson or not salesperson.is_active:
            continue
        groups: list[tuple[str, list[dict]]] = sorted(bucket["customers"].items())
        if bucket["backorder"]:
            groups.append(("Trio Back Order", bucket["backorder"]))
        if not groups:
            continue

        line_parts = {ln["mpn"] for _, lines_ in groups for ln in lines_}
        digest_dups = [g for g in dup_groups if any(p in line_parts for p in g)]

        digest = ProactiveDigest(
            salesperson_id=salesperson_id,
            status=ProactiveDigestStatus.DRAFT,
            subject=DIGEST_SUBJECT,
            body_html=_render_body(salesperson, groups, digest_dups),
            generated_at=datetime.now(UTC),
        )
        db.add(digest)
        db.flush()
        for _, group_lines in groups:
            for line in group_lines:
                quote = line["quote_anchor"] or {}
                win = line["win_anchor"] or {}
                db.add(
                    ProactiveOutreachLine(
                        digest_id=digest.id,
                        match_id=line["match_id"],
                        mpn=line["mpn"],
                        company_id=line["company_id"],
                        salesperson_id=salesperson_id,
                        last_asked_at=line["last_asked_at"],
                        last_asked_qty=line["last_asked_qty"],
                        requirement_count=line["requirement_count"],
                        available_qty=line["available_qty"],
                        low_cost=line["low_cost"],
                        last_quote_price=quote.get("price"),
                        last_quote_at=quote.get("at"),
                        last_quote_company=quote.get("company"),
                        last_quote_rep=quote.get("rep"),
                        last_win_price=win.get("price"),
                        last_win_at=win.get("at"),
                        last_win_company=win.get("company"),
                        last_win_rep=win.get("rep"),
                    )
                )
        generated += 1

    logger.info("Proactive digest generation: {} draft(s) from {} match(es)", generated, len(matches))
    return {"digests_generated": generated, "matches_considered": len(matches)}


# ── Send (human-triggered, actor's token) ────────────────────────────────


async def send_digest(db: Session, digest_id: int, actor: User, token: str) -> None:
    """Mail a DRAFT digest to its salesperson using the ACTOR's delegated token.

    Raises ValueError on wrong status / missing recipient. Marks the digest SENT and
    stamps each line's sent_at (the tracking clock). Caller commits.
    """
    from ..utils.graph_client import GraphClient

    digest = db.get(ProactiveDigest, digest_id)
    if not digest or digest.status != ProactiveDigestStatus.DRAFT:
        raise ValueError("Digest not found or not a draft")
    salesperson = db.get(User, digest.salesperson_id)
    if not salesperson or not salesperson.email:
        raise ValueError("Digest has no recipient")

    # Atomic claim (QC 2026-08-14): flip DRAFT→SENT in one conditional UPDATE and commit
    # BEFORE the Graph send, so the generate_digests "clear ALL drafts" sweep (a separate
    # job session) can no longer delete this digest mid-send, and a concurrent send_digest
    # can't double-mail it. Only the caller whose UPDATE matches status=DRAFT wins; a send
    # failure reverts the claim to DRAFT so it can be retried.
    now = datetime.now(UTC)
    claimed = db.execute(
        update(ProactiveDigest)
        .where(ProactiveDigest.id == digest_id, ProactiveDigest.status == ProactiveDigestStatus.DRAFT)
        .values(status=ProactiveDigestStatus.SENT, sent_at=now, sent_by_id=actor.id)
    ).rowcount
    db.commit()
    if not claimed:
        raise ValueError("Digest not found or not a draft")
    db.refresh(digest)

    gc = GraphClient(token)
    try:
        await gc.post_json(
            "/me/sendMail",
            {
                "message": {
                    "subject": digest.subject,
                    "body": {"contentType": "HTML", "content": digest.body_html},
                    "toRecipients": [{"emailAddress": {"address": salesperson.email}}],
                },
                "saveToSentItems": "true",
            },
        )
    except Exception:
        # Revert the claim so the digest can be retried with one click.
        digest.status = ProactiveDigestStatus.DRAFT
        digest.sent_at = None
        digest.sent_by_id = None
        db.commit()
        raise
    db.execute(update(ProactiveOutreachLine).where(ProactiveOutreachLine.digest_id == digest.id).values(sent_at=now))
    logger.info("Proactive digest {} sent to {} by {}", digest.id, salesperson.email, actor.email)


# ── Review-pane view + line tracking ─────────────────────────────────────


def get_digests_for_view(db: Session, *, viewer: User, can_see_all: bool) -> list[dict]:
    """Template-ready digest cards: drafts first, newest first, lines included.

    Managers/admins see every salesperson's digests (they review and send);
    salespeople see their own. Line tracking cells are editable by the line's
    salesperson and by managers/admins.
    """
    q = select(ProactiveDigest).order_by(ProactiveDigest.status.asc(), ProactiveDigest.generated_at.desc())
    if not can_see_all:
        q = q.where(ProactiveDigest.salesperson_id == viewer.id)
    digests = db.scalars(q).all()

    user_names = dict(db.execute(select(User.id, User.name)).all())
    company_names = dict(db.execute(select(Company.id, Company.name)).all())

    out = []
    for d in digests:
        lines = db.scalars(
            select(ProactiveOutreachLine)
            .where(ProactiveOutreachLine.digest_id == d.id)
            .order_by(ProactiveOutreachLine.company_id.nullslast(), ProactiveOutreachLine.mpn)
        ).all()
        out.append(
            {
                "id": d.id,
                "status": d.status,
                "salesperson_name": user_names.get(d.salesperson_id) or "—",
                "sent_by_name": user_names.get(d.sent_by_id) or "—",
                "generated_at_display": fmt_date(d.generated_at),
                "sent_at_display": fmt_date(d.sent_at),
                "body_html": d.body_html,
                "line_count": len(lines),
                "lines": [
                    {
                        "id": line.id,
                        "mpn": line.mpn,
                        "company_name": company_names.get(line.company_id),
                        "contacted": line.contacted,
                        "outcome": line.outcome,
                        "sales_order_number": line.sales_order_number,
                        "can_edit": can_see_all or line.salesperson_id == viewer.id,
                    }
                    for line in lines
                ],
            }
        )
    return out


def update_line_tracking(
    db: Session,
    line_id: int,
    *,
    viewer: User,
    can_manage: bool,
    contacted: bool | None = None,
    outcome: str | None = None,
    sales_order_number: str | None = None,
) -> ProactiveOutreachLine:
    """Record what came back on one sent digest line. Caller commits.

    Editable by the line's salesperson and managers/admins. Every change is
    written to the ChangeLog audit trail. ``sales_order_number`` is an external
    ERP document reference only. Raises ValueError on missing line / not-sent /
    bad outcome / no permission.
    """
    from ..constants import ProactiveOutreachOutcome
    from ..models import ChangeLog

    line = db.get(ProactiveOutreachLine, line_id)
    if not line:
        raise ValueError("Line not found")
    if not line.sent_at:
        raise ValueError("Line not sent yet — tracking starts after send")
    if not can_manage and line.salesperson_id != viewer.id:
        raise ValueError("Not your line")

    def _log(field: str, old, new) -> None:
        db.add(
            ChangeLog(
                entity_type="proactive_outreach_line",
                entity_id=line.id,
                user_id=viewer.id,
                field_name=field,
                old_value=str(old) if old is not None else None,
                new_value=str(new) if new is not None else None,
            )
        )

    if contacted is not None and contacted != line.contacted:
        _log("contacted", line.contacted, contacted)
        line.contacted = contacted
    if outcome is not None:
        outcome = outcome.strip() or None
        if outcome and outcome not in {o.value for o in ProactiveOutreachOutcome}:
            raise ValueError(f"Unknown outcome: {outcome}")
        if outcome != line.outcome:
            _log("outcome", line.outcome, outcome)
            line.outcome = outcome
            if outcome and outcome != ProactiveOutreachOutcome.NO_RESPONSE:
                if not line.contacted:
                    _log("contacted", line.contacted, True)
                line.contacted = True  # a real answer implies contact was made
    if sales_order_number is not None:
        sales_order_number = sales_order_number.strip() or None
        if sales_order_number != line.sales_order_number:
            _log("sales_order_number", line.sales_order_number, sales_order_number)
            line.sales_order_number = sales_order_number
    return line


# ── Weekly outreach summary (Step 8) ─────────────────────────────────────


def weekly_outreach_summary(db: Session, *, days: int = 7) -> dict:
    """Manager view: is the outreach happening at all?

    Over lines SENT in the trailing window: lines sent / contacted, contact
    rate by salesperson, requirements reopened, quotes issued, orders won.
    """
    cutoff_dt = datetime.now(UTC) - timedelta(days=days)
    lines = db.execute(
        select(ProactiveOutreachLine, User.name)
        .outerjoin(User, User.id == ProactiveOutreachLine.salesperson_id)
        .where(ProactiveOutreachLine.sent_at >= cutoff_dt)
    ).all()
    by_rep: dict[str, dict] = {}
    totals = {
        "lines_sent": 0,
        "lines_contacted": 0,
        "requirements_reopened": 0,
        "quotes_issued": 0,
        "orders_won": 0,
    }
    for line, rep_name in lines:
        rep = rep_name or "—"
        bucket = by_rep.setdefault(rep, {"sent": 0, "contacted": 0})
        bucket["sent"] += 1
        totals["lines_sent"] += 1
        if line.contacted:
            bucket["contacted"] += 1
            totals["lines_contacted"] += 1
        if line.produced_requisition_id:
            totals["requirements_reopened"] += 1
        if line.produced_quote_id:
            totals["quotes_issued"] += 1
        if line.sales_order_number:
            totals["orders_won"] += 1
    for bucket in by_rep.values():
        bucket["contact_rate"] = round(bucket["contacted"] / bucket["sent"] * 100, 1) if bucket["sent"] else 0.0
    totals["contact_rate"] = (
        round(totals["lines_contacted"] / totals["lines_sent"] * 100, 1) if totals["lines_sent"] else 0.0
    )
    return {"totals": totals, "by_salesperson": dict(sorted(by_rep.items()))}
