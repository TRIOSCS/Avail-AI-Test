"""tests/test_qualification_signals.py — vendor-risk flags + fresh ai_flags at review.

Phase 1 of the Part/Offer Qualification build (spec v5): the buy-plan review must show
vendor/offer red flags computed FRESH (not the stale build-time snapshot), and vendor
risk must come from ONE shared computation so the offer Pre-check and the plan flags
can't disagree.

Covers:
  - vendor_safety_for_card: the shared card-only vendor-risk computation.
  - generate_ai_flags: emits a vendor_risk flag for a blacklisted / high-cancellation
    vendor, keyed to the line.
  - _sheet_ctx (render context): ai_flags are recomputed on render (compute-on-read),
    so a change after build is reflected — the freshness fix.

Called by: pytest
Depends on: app.services.sourcing_leads, app.services.buyplan_builder,
    app.routers.htmx.approvals_hub, the builders in tests.test_buyplan_builder_extra.
"""

import os

os.environ["TESTING"] = "1"

from sqlalchemy.orm import Session

from app.services.buyplan_builder import generate_ai_flags
from app.services.sourcing_leads import vendor_safety_for_card
from tests.test_buyplan_builder_extra import (
    _make_company,
    _make_offer,
    _make_plan_with_line,
    _make_quote,
    _make_requirement,
    _make_requisition,
    _make_site,
    _make_user,
    _make_vendor,
)

# ── vendor_safety_for_card (the shared computation) ───────────────────────


def test_safety_flags_high_cancellation(db_session: Session):
    v = _make_vendor(db_session)
    v.cancellation_rate = 0.5  # > 0.2 threshold
    db_session.flush()
    out = vendor_safety_for_card(db_session, v)
    assert "high_cancellation_rate" in out["caution"]
    assert out["band"] in ("low_risk", "medium_risk", "high_risk")


def test_safety_flags_blacklist_do_not_contact(db_session: Session):
    v = _make_vendor(db_session)
    v.is_blacklisted = True
    db_session.flush()
    out = vendor_safety_for_card(db_session, v)
    assert "internal_do_not_contact_history" in out["caution"]


def test_safety_no_card_is_unknown(db_session: Session):
    out = vendor_safety_for_card(db_session, None)
    assert out["band"] == "unknown"
    assert "no_internal_vendor_profile" in out["caution"]


def test_safety_positives_stripped_from_caution(db_session: Session):
    v = _make_vendor(db_session, name="Good Vendor")
    v.website = "https://goodvendor.com"
    v.domain = "goodvendor.com"
    v.emails = ["sales@goodvendor.com"]
    db_session.flush()
    out = vendor_safety_for_card(db_session, v)
    # positives are reported separately, never as caution codes
    assert all(not c.startswith("positive:") for c in out["caution"])
    assert any("website" in p or "footprint" in p or "channels" in p for p in out["positives"])


# ── generate_ai_flags emits a per-line vendor_risk flag ───────────────────


def _plan_with_vendor(db, *, blacklisted=False, cancel=None):
    user = _make_user(db)
    company = _make_company(db)
    site = _make_site(db, company)
    req = _make_requisition(db, user, site)
    requirement = _make_requirement(db, req)
    vendor = _make_vendor(db, name="Risk Vendor")
    if blacklisted:
        vendor.is_blacklisted = True
    if cancel is not None:
        vendor.cancellation_rate = cancel
    db.flush()
    offer = _make_offer(db, req, requirement, vendor)
    quote = _make_quote(db, req, site, user)
    plan, line = _make_plan_with_line(db, quote, req, requirement, offer, buyer_id=user.id)
    return plan, line


def test_generate_flags_emits_vendor_risk_for_blacklist(db_session: Session):
    plan, line = _plan_with_vendor(db_session, blacklisted=True)
    flags = generate_ai_flags(plan, db_session)
    vr = [f for f in flags if f["type"] == "vendor_risk"]
    assert vr, "expected a vendor_risk flag for a blacklisted vendor"
    assert vr[0]["severity"] == "critical"
    assert vr[0]["line_id"] == line.id


def test_generate_flags_vendor_risk_for_high_cancellation(db_session: Session):
    plan, line = _plan_with_vendor(db_session, cancel=0.6)
    vr = [f for f in generate_ai_flags(plan, db_session) if f["type"] == "vendor_risk"]
    assert vr and vr[0]["severity"] == "warning"


def test_generate_flags_no_vendor_risk_for_clean_vendor(db_session: Session):
    plan, _ = _plan_with_vendor(db_session)  # clean vendor
    vr = [f for f in generate_ai_flags(plan, db_session) if f["type"] == "vendor_risk"]
    assert not vr


# ── freshness: _sheet_ctx recomputes on render (compute-on-read) ──────────


def test_sheet_ctx_ai_flags_are_fresh(db_session: Session, test_user):
    from app.routers.htmx.approvals_hub import _sheet_ctx

    plan, line = _plan_with_vendor(db_session)
    # Stored snapshot is deliberately WRONG/empty; the render must ignore it.
    plan.ai_flags = [{"type": "stale_snapshot", "severity": "warning", "line_id": 0, "message": "old"}]
    db_session.flush()
    # Now make the vendor risky AFTER "build" — a fresh render must catch it.
    line.offer.vendor_card.is_blacklisted = True
    db_session.flush()

    ctx = _sheet_ctx(db_session, test_user, plan, is_sourcing=True)
    types = {f["type"] for f in ctx["ai_flags"]}
    assert "vendor_risk" in types, "fresh compute should catch the post-build risk"
    assert "stale_snapshot" not in types, "must not read the stale stored ai_flags"
    assert ctx["ai_flags_by_line"].get(line.id), "flags indexed per line for the row count"
