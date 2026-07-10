"""Consolidated authz-hardening regression suite (pre-multi-user launch blocker).

Single security-batch file for the 9 fix groups / 13 routes hardened on
`fix/authz-hardening`. Each gate REUSES an existing helper from app/dependencies.py
(can_manage_account / can_manage_account_team / require_requisition_access /
get_buyplan_for_user / is_manager_or_admin) — these tests lock in the boundary:

  - a cross-account / restricted-role actor is DENIED (403 for account gates, 404 for
    requisition-ownership gates so existence isn't leaked), with NO mutation, and
  - a legitimate owner / manager / admin is ALLOWED,
  - and for proactive / follow-ups / quote we additionally assert the OTHER owner's data
    is untouched.

The shared `client` fixture overrides require_user to return `test_user`; we mutate that
same object's role / re-own resources to admin_user to simulate a restricted non-owner.

Called by: pytest
Depends on: app.routers.{ai,proactive,sources,htmx_views,quality_plans,crm.quotes},
            app.services.prepayment_service, conftest fixtures.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.config import settings
from app.constants import UserRole
from app.models import (
    Company,
    CustomerSite,
    Offer,
    ProactiveDoNotOffer,
    ProactiveMatch,
    ProspectContact,
    Requirement,
    Requisition,
)
from app.models.buy_plan import BuyPlan
from app.models.offers import Contact as RfqContact
from app.models.offers import VendorResponse
from app.models.quality_plan import QualityPlan
from app.models.quotes import Quote

# ── Shared builders ──────────────────────────────────────────────────────


def _now():
    return datetime.now(UTC)


def _make_sales(test_user, db_session):
    """Flip the request actor to a restricted SALES role (observed at request time)."""
    test_user.role = UserRole.SALES
    db_session.commit()


def _make_trader(test_user, db_session):
    test_user.role = UserRole.TRADER
    db_session.commit()


def _foreign_company(db_session, admin_user):
    """A company owned by admin_user (not the request actor)."""
    co = Company(name="Foreign Owned Co", is_active=True, account_owner_id=admin_user.id, created_at=_now())
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


def _owned_company(db_session, test_user):
    """A company whose primary account owner IS the request actor."""
    co = Company(name="My Owned Co", is_active=True, account_owner_id=test_user.id, created_at=_now())
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


def _site(db_session, company, *, owner_id=None):
    site = CustomerSite(company_id=company.id, site_name="HQ", owner_id=owner_id)
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


def _requisition(db_session, owner_id, *, name="REQ-AUTHZ", site_id=None):
    req = Requisition(
        name=name,
        customer_name="Cust",
        status="active",
        created_by=owner_id,
        customer_site_id=site_id,
        created_at=_now(),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(requisition_id=req.id, primary_mpn="LM317T", target_qty=100, created_at=_now())
    db_session.add(item)
    db_session.commit()
    db_session.refresh(req)
    return req


# ══════════════════════════════════════════════════════════════════════════
# FIX GROUP 1 — edit_company owner reassignment needs the TEAM gate
# Route: POST /v2/partials/customers/{company_id}/edit  (htmx.companies.edit_company)
# Helper: can_manage_account_team
# ══════════════════════════════════════════════════════════════════════════


def test_g1_site_owner_cannot_seize_primary_ownership(client, db_session, test_user, admin_user):
    """A site-owner passes can_manage_account but fails _team → 403; owner unchanged."""
    co = _foreign_company(db_session, admin_user)  # primary owner = admin_user
    _site(db_session, co, owner_id=test_user.id)  # test_user is only a SITE owner → can_manage_account True
    resp = client.post(
        f"/v2/partials/customers/{co.id}/edit",
        data={"name": co.name, "owner_id": str(test_user.id)},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 403
    db_session.refresh(co)
    assert co.account_owner_id == admin_user.id  # ownership NOT seized


def test_g1_site_owner_cannot_change_hierarchy(client, db_session, test_user, admin_user):
    """A site-owner (not team) cannot restructure parent company → 403."""
    parent = _foreign_company(db_session, admin_user)
    co = Company(name="Child Co", is_active=True, account_owner_id=admin_user.id, created_at=_now())
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    _site(db_session, co, owner_id=test_user.id)
    resp = client.post(
        f"/v2/partials/customers/{co.id}/edit",
        data={"name": co.name, "parent_company_id": str(parent.id)},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 403
    db_session.refresh(co)
    assert co.parent_company_id is None


def test_g1_primary_owner_can_reassign(client, db_session, test_user, admin_user):
    """The primary account owner CAN reassign primary ownership → 200."""
    co = _owned_company(db_session, test_user)  # test_user is primary owner
    resp = client.post(
        f"/v2/partials/customers/{co.id}/edit",
        data={"name": co.name, "owner_id": str(admin_user.id)},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    db_session.refresh(co)
    assert co.account_owner_id == admin_user.id


def test_g1_manager_can_reassign(client, db_session, test_user, admin_user):
    """A manager may reassign even an account they don't own → 200."""
    test_user.role = UserRole.MANAGER
    db_session.commit()
    co = _foreign_company(db_session, admin_user)
    resp = client.post(
        f"/v2/partials/customers/{co.id}/edit",
        data={"name": co.name, "owner_id": str(test_user.id)},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    db_session.refresh(co)
    assert co.account_owner_id == test_user.id


# ══════════════════════════════════════════════════════════════════════════
# FIX GROUP 2 — ai.py site-linked prospect records need can_manage_account
# Routes: DELETE /api/ai/prospect-contacts/{id}  (2a)
#         POST   /api/ai/prospect-contacts/{id}/save  (2b)
#         POST   /api/ai/apply-freeform-rfq  (2c)
# Helper: can_manage_account
# ══════════════════════════════════════════════════════════════════════════


def _site_linked_pc(db_session, site):
    pc = ProspectContact(
        customer_site_id=site.id,
        full_name="Jane Doe",
        email="jane@x.com",
        source="web_search",
        confidence="high",
    )
    db_session.add(pc)
    db_session.commit()
    db_session.refresh(pc)
    return pc


def test_g2a_delete_site_prospect_blocks_non_owner(client, db_session, test_user, admin_user):
    co = _foreign_company(db_session, admin_user)
    site = _site(db_session, co)  # no site owner → test_user (buyer, not manager) cannot manage
    pc = _site_linked_pc(db_session, site)
    resp = client.delete(f"/api/ai/prospect-contacts/{pc.id}")
    assert resp.status_code == 403
    assert db_session.get(ProspectContact, pc.id) is not None  # NOT deleted


def test_g2b_save_site_prospect_blocks_non_owner(client, db_session, test_user, admin_user):
    co = _foreign_company(db_session, admin_user)
    site = _site(db_session, co)
    pc = _site_linked_pc(db_session, site)
    resp = client.post(f"/api/ai/prospect-contacts/{pc.id}/save")
    assert resp.status_code == 403
    db_session.refresh(pc)
    assert pc.is_saved is False  # NOT mutated


def test_g2_site_prospect_allows_account_owner(client, db_session, test_user):
    co = _owned_company(db_session, test_user)  # test_user is account owner
    site = _site(db_session, co)
    pc = _site_linked_pc(db_session, site)
    resp = client.post(f"/api/ai/prospect-contacts/{pc.id}/save")
    assert resp.status_code == 200
    db_session.refresh(pc)
    assert pc.is_saved is True


def test_g2_vendor_linked_prospect_stays_global(client, db_session, test_user, test_vendor_card):
    """Vendor-linked prospects have no account owner — the gate is a no-op (200)."""
    pc = ProspectContact(
        vendor_card_id=test_vendor_card.id,
        full_name="Vendor Rep",
        email="rep@vendor.com",
        source="web_search",
        confidence="high",
    )
    db_session.add(pc)
    db_session.commit()
    db_session.refresh(pc)
    resp = client.post(f"/api/ai/prospect-contacts/{pc.id}/save")
    assert resp.status_code == 200


def test_g2c_apply_freeform_rfq_blocks_non_owner(client, db_session, test_user, admin_user):
    co = _foreign_company(db_session, admin_user)
    site = _site(db_session, co)
    resp = client.post(
        "/api/ai/apply-freeform-rfq",
        json={
            "name": "RFQ",
            "customer_name": "Cust",
            "customer_site_id": site.id,
            "requirements": [{"primary_mpn": "LM317T", "target_qty": 10}],
        },
    )
    assert resp.status_code == 403


def test_g2c_apply_freeform_rfq_allows_owner(client, db_session, test_user):
    co = _owned_company(db_session, test_user)
    site = _site(db_session, co)
    with (
        patch("app.services.ai_offer_service.apply_freeform_rfq") as mock_apply,
        patch("app.cache.decorators.invalidate_prefix"),
    ):
        mock_apply.return_value = {"requisition_id": 1, "requirements_created": 1}
        resp = client.post(
            "/api/ai/apply-freeform-rfq",
            json={
                "name": "RFQ",
                "customer_name": "Cust",
                "customer_site_id": site.id,
                "requirements": [{"primary_mpn": "LM317T", "target_qty": 10}],
            },
        )
    assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# FIX GROUP 3 — proactive do-not-offer per-account authz + per-owner scope
# Route: POST /api/proactive/do-not-offer  (proactive.add_do_not_offer)
# Helper: can_manage_account  (+ salesperson_id scope on auto-dismiss)
# ══════════════════════════════════════════════════════════════════════════


def _proactive_match(db_session, *, company, salesperson_id, mpn="LM317T", status="new"):
    """A complete ProactiveMatch (offer_id/requirement_id are NOT NULL) for the given
    owner."""
    req = _requisition(db_session, salesperson_id, name=f"REQ-PM-{salesperson_id}")
    item = db_session.query(Requirement).filter_by(requisition_id=req.id).first()
    site = _site(db_session, company)
    offer = Offer(
        requisition_id=req.id,
        requirement_id=item.id,
        vendor_name="V",
        mpn=mpn,
        unit_price=Decimal("1.00"),
        qty_available=10,
        entered_by_id=salesperson_id,
        status="active",
        created_at=_now(),
    )
    db_session.add(offer)
    db_session.flush()
    match = ProactiveMatch(
        offer_id=offer.id,
        requirement_id=item.id,
        requisition_id=req.id,
        customer_site_id=site.id,
        salesperson_id=salesperson_id,
        company_id=company.id,
        mpn=mpn,
        status=status,
        created_at=_now(),
    )
    db_session.add(match)
    db_session.commit()
    db_session.refresh(match)
    return match


def test_g3_do_not_offer_blocks_non_owner_and_preserves_other_matches(client, db_session, test_user, admin_user):
    """Non-owner do-not-offer → 403, no DNO row, the owner's 'new' matches untouched."""
    co = _foreign_company(db_session, admin_user)  # owned by admin_user, not test_user
    # admin_user's open match for the same mpn+company — must survive
    owner_match = _proactive_match(db_session, company=co, salesperson_id=admin_user.id)

    resp = client.post(
        "/api/proactive/do-not-offer",
        json={"items": [{"mpn": "LM317T", "company_id": co.id}]},
    )
    assert resp.status_code == 403
    # No suppression row created
    assert db_session.query(ProactiveDoNotOffer).filter_by(company_id=co.id).count() == 0
    # The rightful owner's open match is untouched
    db_session.refresh(owner_match)
    assert owner_match.status == "new"


def test_g3_do_not_offer_allows_owner_and_dismisses_only_own_match(client, db_session, test_user, admin_user):
    """Owner → 200; auto-dismiss scoped to salesperson_id (other owner's match
    survives)."""
    co = _owned_company(db_session, test_user)
    my_match = _proactive_match(db_session, company=co, salesperson_id=test_user.id)
    other_match = _proactive_match(db_session, company=co, salesperson_id=admin_user.id)

    resp = client.post(
        "/api/proactive/do-not-offer",
        json={"items": [{"mpn": "LM317T", "company_id": co.id}]},
    )
    assert resp.status_code == 200
    assert resp.json()["suppressed"] == 1
    db_session.refresh(my_match)
    db_session.refresh(other_match)
    assert my_match.status == "dismissed"  # own match dismissed
    assert other_match.status == "new"  # the OTHER owner's match untouched


# ══════════════════════════════════════════════════════════════════════════
# FIX GROUP 4 — email-mining parse-response-attachments needs req ownership
# Route: POST /api/email-mining/parse-response-attachments/{response_id}
# Helper: require_requisition_access
# ══════════════════════════════════════════════════════════════════════════


def _vendor_response(db_session, owner_id, *, message_id="msg-x"):
    req = _requisition(db_session, owner_id, name="REQ-VR")
    vr = VendorResponse(
        requisition_id=req.id,
        vendor_name="ACME",
        vendor_email="s@acme.com",
        subject="re",
        message_id=message_id,
        status="new",
        received_at=_now(),
        created_at=_now(),
    )
    db_session.add(vr)
    db_session.commit()
    db_session.refresh(vr)
    return vr


def _connect_m365(test_user, db_session):
    """The parse route gates on M365 connection BEFORE the ownership check; satisfy it
    so the ownership boundary is the thing under test."""
    test_user.m365_connected = True
    test_user.access_token = "test-token"
    db_session.commit()


def test_g4_parse_attachments_blocks_non_owner_sales(client, db_session, test_user, admin_user):
    vr = _vendor_response(db_session, admin_user.id)  # foreign requisition
    _connect_m365(test_user, db_session)
    _make_sales(test_user, db_session)
    resp = client.post(f"/api/email-mining/parse-response-attachments/{vr.id}")
    assert resp.status_code == 404


def test_g4_parse_attachments_blocks_non_owner_trader(client, db_session, test_user, admin_user):
    vr = _vendor_response(db_session, admin_user.id)
    _connect_m365(test_user, db_session)
    _make_trader(test_user, db_session)
    resp = client.post(f"/api/email-mining/parse-response-attachments/{vr.id}")
    assert resp.status_code == 404


def test_g4_parse_attachments_owner_passes_ownership_gate(client, db_session, test_user):
    """Owner is past the ownership gate; later logic 400s (no message_id) — NOT a
    404."""
    vr = _vendor_response(db_session, test_user.id, message_id=None)
    _connect_m365(test_user, db_session)
    _make_sales(test_user, db_session)  # restricted but OWNS the requisition
    resp = client.post(f"/api/email-mining/parse-response-attachments/{vr.id}")
    assert resp.status_code != 404  # passed the ownership gate (400 from missing message_id)


# ══════════════════════════════════════════════════════════════════════════
# FIX GROUP 5 — prepayment creation needs buy-plan ownership (service layer)
# Route: POST /v2/prepayments  (prepayments.post_prepayment → create_prepayment)
# Helper: get_buyplan_for_user
# ══════════════════════════════════════════════════════════════════════════


def _buy_plan(db_session, owner_id):
    req = _requisition(db_session, owner_id, name="REQ-PP")
    quote = Quote(
        requisition_id=req.id,
        quote_number="Q-PP",
        status="sent",
        line_items=[],
        created_by_id=owner_id,
        created_at=_now(),
    )
    db_session.add(quote)
    db_session.flush()
    bp = BuyPlan(quote_id=quote.id, requisition_id=req.id, status="draft", so_status="pending")
    db_session.add(bp)
    db_session.commit()
    db_session.refresh(bp)
    return bp


def test_g5_prepayment_blocks_restricted_non_owner_sales(client, db_session, test_user, admin_user):
    bp = _buy_plan(db_session, admin_user.id)  # foreign buy plan
    _make_sales(test_user, db_session)
    resp = client.post(
        "/v2/prepayments",
        json={"buy_plan_id": bp.id, "total_incl_fees": "100.00"},
    )
    assert resp.status_code == 404
    # No prepayment attached to the foreign plan
    from app.models.quality_plan import Prepayment

    assert db_session.query(Prepayment).filter_by(buy_plan_id=bp.id).count() == 0


def test_g5_prepayment_allows_buyer_owner(client, db_session, test_user):
    bp = _buy_plan(db_session, test_user.id)  # test_user (buyer) owns the requisition
    resp = client.post(
        "/v2/prepayments",
        json={"buy_plan_id": bp.id, "total_incl_fees": "100.00"},
    )
    # Past the ownership gate. Routing may raise NoEligibleApprover (no approvers seeded);
    # the security boundary is simply that it is NOT a 404 buy-plan-ownership rejection.
    assert resp.status_code != 404


# ══════════════════════════════════════════════════════════════════════════
# FIX GROUP 6 — htmx requisition-scoped mutations need require_requisition_access
# Routes: 6a POST /v2/partials/sourcing/{requirement_id}/search
#         6b POST /v2/partials/follow-ups/send-batch (+ follow_up_badge scope)
#         6c POST /v2/partials/requisitions/{req_id}/ai-rephrase-email
# Helpers: require_requisition_access / RESTRICTED_ROLES scope
# ══════════════════════════════════════════════════════════════════════════


def test_g6a_sourcing_search_blocks_non_owner_sales(client, db_session, test_user, admin_user):
    req = _requisition(db_session, admin_user.id, name="REQ-SRC")
    item = db_session.query(Requirement).filter_by(requisition_id=req.id).first()
    _make_sales(test_user, db_session)
    resp = client.post(f"/v2/partials/sourcing/{item.id}/search")
    assert resp.status_code == 404


def test_g6c_ai_rephrase_email_blocks_non_owner_sales(client, db_session, test_user, admin_user):
    req = _requisition(db_session, admin_user.id, name="REQ-REPH")
    _make_sales(test_user, db_session)
    resp = client.post(
        f"/v2/partials/requisitions/{req.id}/ai-rephrase-email",
        data={"body": "hello"},
    )
    assert resp.status_code == 404


def test_g6b_send_batch_leaves_other_owners_stale_contacts_untouched(client, db_session, test_user, admin_user):
    """A SALES user's send-batch must not touch another owner's stale RfqContact."""
    stale_at = _now() - __import__("datetime").timedelta(days=30)
    # Another owner's requisition + a stale 'sent' email contact under it
    other_req = _requisition(db_session, admin_user.id, name="REQ-OTHER-FU")
    other_contact = RfqContact(
        requisition_id=other_req.id,
        user_id=admin_user.id,
        contact_type="email",
        vendor_name="OtherVendor",
        status="sent",
        created_at=stale_at,
    )
    db_session.add(other_contact)
    db_session.commit()

    _make_sales(test_user, db_session)  # restricted role, owns nothing here
    resp = client.post("/v2/partials/follow-ups/send-batch")
    assert resp.status_code == 200
    db_session.refresh(other_contact)
    # The other owner's stale contact was NOT acted on by this user's batch
    assert other_contact.status == "sent"


# ══════════════════════════════════════════════════════════════════════════
# FIX GROUP 7 — create_quote must scope offer_ids to the target requisition
# Route: POST /api/requisitions/{req_id}/quote  (crm.quotes.create_quote)
# Helper: Offer.requisition_id == req_id filter (+ on_quote_built scope)
# ══════════════════════════════════════════════════════════════════════════


def test_g7_create_quote_rejects_foreign_offer_and_leaves_req_b_untouched(client, db_session, test_user, admin_user):
    """Owner of req A cannot pull a foreign offer (req B) into the quote → 400; req B's
    requirement status unchanged."""
    site = _site(db_session, _owned_company(db_session, test_user))
    req_a = _requisition(db_session, test_user.id, name="REQ-A", site_id=site.id)
    req_b = _requisition(db_session, admin_user.id, name="REQ-B")
    item_b = db_session.query(Requirement).filter_by(requisition_id=req_b.id).first()
    status_before = item_b.sourcing_status

    foreign_offer = Offer(
        requisition_id=req_b.id,
        requirement_id=item_b.id,
        vendor_name="V",
        mpn="LM317T",
        unit_price=Decimal("1.00"),
        qty_available=10,
        entered_by_id=admin_user.id,
        status="active",
        created_at=_now(),
    )
    db_session.add(foreign_offer)
    db_session.commit()

    resp = client.post(
        f"/api/requisitions/{req_a.id}/quote",
        json={"offer_ids": [foreign_offer.id], "line_items": []},
    )
    assert resp.status_code == 400  # offer does not belong to this requisition
    # No quote created for req A
    assert db_session.query(Quote).filter_by(requisition_id=req_a.id).count() == 0
    # Req B's requirement was NOT advanced to 'quoted'
    db_session.refresh(item_b)
    assert item_b.sourcing_status == status_before


def test_g7_create_quote_allows_own_offer(client, db_session, test_user):
    site = _site(db_session, _owned_company(db_session, test_user))
    req = _requisition(db_session, test_user.id, name="REQ-OWN", site_id=site.id)
    item = db_session.query(Requirement).filter_by(requisition_id=req.id).first()
    own_offer = Offer(
        requisition_id=req.id,
        requirement_id=item.id,
        vendor_name="V",
        mpn="LM317T",
        unit_price=Decimal("1.00"),
        qty_available=10,
        entered_by_id=test_user.id,
        status="active",
        created_at=_now(),
    )
    db_session.add(own_offer)
    db_session.commit()

    resp = client.post(
        f"/api/requisitions/{req.id}/quote",
        json={"offer_ids": [own_offer.id], "line_items": []},
    )
    assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# FIX GROUP 8 — QP submit/read need requisition-ownership scope
# Routes: GET /v2/qp/{qp_id}, POST /v2/qp/{qp_id}/submit  (quality_plans)
# Helper: require_requisition_access (via _require_qp_access)
# ══════════════════════════════════════════════════════════════════════════


def _quality_plan(db_session, owner_id):
    req = _requisition(db_session, owner_id, name="REQ-QP")
    quote = Quote(requisition_id=req.id, quote_number="Q-QP", status="sent", line_items=[], created_at=_now())
    db_session.add(quote)
    db_session.flush()
    bp = BuyPlan(quote_id=quote.id, requisition_id=req.id, status="draft", so_status="pending")
    db_session.add(bp)
    db_session.flush()
    qp = QualityPlan(buy_plan_id=bp.id, created_by_id=owner_id, status="draft", order_type="new")
    db_session.add(qp)
    db_session.commit()
    db_session.refresh(qp)
    return qp


def test_g8_qp_detail_blocks_non_owner_sales(client, db_session, test_user, admin_user):
    qp = _quality_plan(db_session, admin_user.id)
    _make_sales(test_user, db_session)
    resp = client.get(f"/v2/qp/{qp.id}")
    assert resp.status_code == 404


def test_g8_qp_submit_blocks_non_owner_trader(client, db_session, test_user, admin_user):
    qp = _quality_plan(db_session, admin_user.id)
    _make_trader(test_user, db_session)
    resp = client.post(f"/v2/qp/{qp.id}/submit")
    assert resp.status_code == 404
    db_session.refresh(qp)
    assert qp.status == "draft"  # not submitted


def test_g8_qp_detail_allows_owner(client, db_session, test_user):
    qp = _quality_plan(db_session, test_user.id)
    resp = client.get(f"/v2/qp/{qp.id}")
    assert resp.status_code == 200


def test_g8_qp_detail_allows_manager(client, db_session, test_user, admin_user):
    qp = _quality_plan(db_session, admin_user.id)  # owned by someone else
    test_user.role = UserRole.MANAGER
    db_session.commit()
    resp = client.get(f"/v2/qp/{qp.id}")
    assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# FIX GROUP 9 — create_company owner assignment needs manager gate + active user
# Route: POST /v2/partials/customers/create  (htmx.companies.create_company)
# Helper: is_manager_or_admin (+ active-user validation)
# ══════════════════════════════════════════════════════════════════════════


def test_g9_rep_cannot_assign_other_owner(client, db_session, test_user, admin_user):
    """A plain rep (buyer) assigning another user as owner → 403."""
    resp = client.post(
        "/v2/partials/customers/create",
        data={"name": "G9 RepBlocked Co", "owner_id": str(admin_user.id)},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 403
    assert db_session.query(Company).filter_by(name="G9 RepBlocked Co").count() == 0


def test_g9_rep_can_assign_self(client, db_session, test_user):
    resp = client.post(
        "/v2/partials/customers/create",
        data={"name": "G9 RepSelf Co", "owner_id": str(test_user.id)},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    co = db_session.query(Company).filter_by(name="G9 RepSelf Co").first()
    assert co is not None and co.account_owner_id == test_user.id


def test_g9_manager_inactive_owner_rejected(client, db_session, test_user, admin_user):
    """Manager assigning an INACTIVE owner → 400."""
    test_user.role = UserRole.MANAGER
    admin_user.is_active = False
    db_session.commit()
    resp = client.post(
        "/v2/partials/customers/create",
        data={"name": "G9 Inactive Co", "owner_id": str(admin_user.id)},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 400
    assert db_session.query(Company).filter_by(name="G9 Inactive Co").count() == 0


def test_g9_manager_can_assign_active_user(client, db_session, test_user, admin_user):
    test_user.role = UserRole.MANAGER
    db_session.commit()
    resp = client.post(
        "/v2/partials/customers/create",
        data={"name": "G9 MgrAssign Co", "owner_id": str(admin_user.id)},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    co = db_session.query(Company).filter_by(name="G9 MgrAssign Co").first()
    assert co is not None and co.account_owner_id == admin_user.id


# ══════════════════════════════════════════════════════════════════════════
# SIBLING ROUTES — htmx_views reaches the SAME mutations as Group 2 / Group 3.
# These were pre-existing un-gated twins of the gated ai.py / proactive.py routes;
# they now reuse the SAME helpers (require_prospect_site_access / can_manage_account).
#
# Concern A — vendor_prospect_{save,delete} on a SITE-linked prospect:
#   POST   /v2/partials/vendors/{vendor_id}/ai/prospect/{prospect_id}/save
#   DELETE /v2/partials/vendors/{vendor_id}/ai/prospect/{prospect_id}
# Concern B — proactive do-not-offer for a foreign company:
#   POST   /v2/partials/proactive/do-not-offer
# ══════════════════════════════════════════════════════════════════════════


def test_siblingA_htmx_vendor_prospect_save_blocks_non_owner(
    client, db_session, test_user, admin_user, test_vendor_card
):
    """A SALES non-owner saving a site-linked prospect via the htmx twin → 403, no
    mutation."""
    _make_sales(test_user, db_session)
    co = _foreign_company(db_session, admin_user)
    site = _site(db_session, co)  # no site owner → SALES non-owner cannot manage
    pc = _site_linked_pc(db_session, site)
    resp = client.post(
        f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/{pc.id}/save",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 403
    db_session.refresh(pc)
    assert pc.is_saved is False  # NOT mutated


def test_siblingA_htmx_vendor_prospect_delete_blocks_non_owner(
    client, db_session, test_user, admin_user, test_vendor_card
):
    """A SALES non-owner deleting a site-linked prospect via the htmx twin → 403, row
    survives."""
    _make_sales(test_user, db_session)
    co = _foreign_company(db_session, admin_user)
    site = _site(db_session, co)
    pc = _site_linked_pc(db_session, site)
    resp = client.delete(
        f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/{pc.id}",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 403
    assert db_session.get(ProspectContact, pc.id) is not None  # NOT deleted


def test_siblingA_htmx_vendor_prospect_save_allows_owner(client, db_session, test_user, test_vendor_card):
    """The account owner saving a site-linked prospect via the htmx twin → 200,
    mutated."""
    co = _owned_company(db_session, test_user)  # test_user is account owner
    site = _site(db_session, co)
    pc = _site_linked_pc(db_session, site)
    resp = client.post(
        f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/{pc.id}/save",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    db_session.refresh(pc)
    assert pc.is_saved is True


def test_siblingA_htmx_vendor_prospect_save_vendor_linked_stays_global(client, db_session, test_user, test_vendor_card):
    """A vendor-linked prospect has no account owner — the gate is a no-op even for
    SALES."""
    _make_sales(test_user, db_session)
    pc = ProspectContact(
        vendor_card_id=test_vendor_card.id,
        full_name="Vendor Rep",
        email="rep@vendor.com",
        source="web_search",
        confidence="high",
    )
    db_session.add(pc)
    db_session.commit()
    db_session.refresh(pc)
    resp = client.post(
        f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/{pc.id}/save",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    db_session.refresh(pc)
    assert pc.is_saved is True


def test_siblingB_htmx_do_not_offer_blocks_non_owner(client, db_session, test_user, admin_user):
    """A SALES non-owner suppressing a foreign company's MPN via the htmx twin → 403, no
    row."""
    _make_sales(test_user, db_session)
    co = _foreign_company(db_session, admin_user)  # owned by admin_user
    resp = client.post(
        "/v2/partials/proactive/do-not-offer",
        data={"mpn": "LM317T", "company_id": str(co.id)},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 403
    assert db_session.query(ProactiveDoNotOffer).filter_by(company_id=co.id).count() == 0


def test_siblingB_htmx_do_not_offer_allows_owner(client, db_session, test_user):
    """The account owner suppressing their own company's MPN via the htmx twin → 200,
    row created."""
    co = _owned_company(db_session, test_user)
    resp = client.post(
        "/v2/partials/proactive/do-not-offer",
        data={"mpn": "lm317t", "company_id": str(co.id)},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    rows = db_session.query(ProactiveDoNotOffer).filter_by(company_id=co.id).all()
    assert len(rows) == 1
    assert rows[0].mpn == "LM317T"  # upper-cased on insert


@pytest.fixture(autouse=True)
def _ai_features_on(monkeypatch):
    """Group-2 ai routes sit behind the AI gate; enable so ownership guards are
    reached."""
    monkeypatch.setattr(settings, "ai_features_enabled", "all")
    yield
