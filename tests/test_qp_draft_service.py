"""test_qp_draft_service.py — TDD for QP draft-from-deal (survey ideas #8+#19).

Drafts the empty free-text Quality-Plan fields from three clearly-separated sources, for
human review (section prefill + amber "AI — verify" banner; nothing writes until the human
accepts and the existing section PATCH saves):
  - deterministic deal-copy   — this deal's Requirement + chosen Offer + commodity (source "deal")
  - carry-forward             — most-recent section-reviewed QP for same company+commodity ("prior")
  - pasted TSO/PO extraction  — Claude reads a pasted customer document ("ai")
Fill-EMPTY-only: a field already set on the QP is never overwritten or re-suggested.

Called by: pytest (TESTING=1 PYTHONPATH=. pytest tests/test_qp_draft_service.py -v)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.models import Company, Requirement, Requisition, User
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.intelligence import MaterialCard
from app.models.offers import Offer
from app.models.quality_plan import QualityPlan

_MPN_SEQ = [0]


def _make_deal(
    db: Session,
    user: User,
    *,
    company_name: str = "Acme Electronics",
    category: str | None = "capacitors",
    req: dict | None = None,
    offer: dict | None = None,
) -> tuple[QualityPlan, BuyPlan, Requirement, Offer]:
    """Assemble
    Company→Requisition→Requirement(+MaterialCard)→Offer→BuyPlan(+line)→QP."""
    _MPN_SEQ[0] += 1
    mpn = f"LM{_MPN_SEQ[0]:04d}"
    # Get-or-create the Company by name so two deals for the "same customer" share one
    # company_id (that FK is how carry-forward matches "same company").
    company = db.query(Company).filter(Company.name == company_name).first()
    if company is None:
        company = Company(name=company_name)
        db.add(company)
        db.flush()
    requisition = Requisition(
        name="REQ", customer_name=company_name, company_id=company.id, status="open", created_by=user.id
    )
    db.add(requisition)
    db.flush()
    card = None
    if category is not None:
        card = MaterialCard(normalized_mpn=mpn, display_mpn=mpn, category=category)
        db.add(card)
        db.flush()
    rq = Requirement(
        requisition_id=requisition.id,
        primary_mpn=mpn,
        material_card_id=card.id if card else None,
        **{"condition": "New", "target_qty": 250, "firmware": "FW1.2", "hardware_codes": "HWA", **(req or {})},
    )
    db.add(rq)
    db.flush()
    off = Offer(
        requisition_id=requisition.id,
        requirement_id=rq.id,
        vendor_name="Arrow",
        vendor_name_normalized="arrow",
        mpn=mpn,
        normalized_mpn=mpn,
        status="active",
        entered_by_id=user.id,
        created_at=datetime.now(UTC),
        **{"condition": "New", "packaging": "Tape & Reel", "firmware": "FW1.2", **(offer or {})},
    )
    db.add(off)
    db.flush()
    bp = BuyPlan(requisition_id=requisition.id, status="draft")
    db.add(bp)
    db.flush()
    db.add(BuyPlanLine(buy_plan_id=bp.id, requirement_id=rq.id, offer_id=off.id, quantity=250, po_number="PO-77"))
    db.flush()
    qp = QualityPlan(created_by_id=user.id, buy_plan_id=bp.id)
    db.add(qp)
    db.commit()
    return qp, bp, rq, off


class TestDeterministicDealCopy:
    def test_sales_draft_fills_empty_fields_from_deal(self, db_session, test_user):
        from app.services.qp_draft_service import build_section_draft

        qp, *_ = _make_deal(db_session, test_user, category="capacitors")
        draft = build_section_draft(db_session, qp, "sales")
        assert draft["sales_condition"]["value"] == "New"
        assert draft["sales_condition"]["source"] == "deal"
        assert draft["sales_quantity"]["value"] == 250
        assert draft["sales_product_commodity"]["value"] == "capacitors"
        assert "FW1.2" in draft["sales_fw_hw_rev"]["value"]
        assert "HWA" in draft["sales_fw_hw_rev"]["value"]

    def test_purchasing_draft_pulls_from_chosen_offer(self, db_session, test_user):
        from app.services.qp_draft_service import build_section_draft

        qp, *_ = _make_deal(db_session, test_user, offer={"packaging": "Tray", "condition": "Refurbished"})
        draft = build_section_draft(db_session, qp, "purchasing")
        assert draft["purchasing_packaging"]["value"] == "Tray"
        assert draft["purchasing_condition"]["value"] == "refurb"  # Offer.condition normalizes to the enum value
        assert draft["purchasing_po_number"]["value"] == "PO-77"
        assert draft["purchasing_product_commodity"]["value"] == "capacitors"

    def test_already_set_field_is_not_suggested(self, db_session, test_user):
        from app.services.qp_draft_service import build_section_draft

        qp, *_ = _make_deal(db_session, test_user)
        qp.sales_condition = "Manually Entered"
        db_session.commit()
        draft = build_section_draft(db_session, qp, "sales")
        assert "sales_condition" not in draft  # never overwrite a human value

    def test_missing_commodity_card_omits_commodity(self, db_session, test_user):
        from app.services.qp_draft_service import build_section_draft

        qp, *_ = _make_deal(db_session, test_user, category=None)
        draft = build_section_draft(db_session, qp, "sales")
        assert "sales_product_commodity" not in draft


class TestCarryForward:
    def test_carry_forward_from_prior_reviewed_qp(self, db_session, test_user):
        from app.services.qp_draft_service import build_section_draft

        # A prior QP for the SAME company+commodity, sales section reviewed, with a policy field set.
        prior, *_ = _make_deal(db_session, test_user, company_name="Acme Electronics", category="capacitors")
        prior.sales_testing_option = "Full functional"
        prior.sales_test_location = "TRIO Lab"
        prior.sales_section_reviewed_at = datetime.now(UTC)
        prior.sales_section_reviewed_by_id = test_user.id
        db_session.commit()

        qp, *_ = _make_deal(db_session, test_user, company_name="Acme Electronics", category="capacitors")
        draft = build_section_draft(db_session, qp, "sales")
        assert draft["sales_testing_option"]["value"] == "Full functional"
        assert draft["sales_testing_option"]["source"] == "prior"
        assert draft["sales_test_location"]["value"] == "TRIO Lab"

    def test_no_carry_forward_across_different_commodity(self, db_session, test_user):
        from app.services.qp_draft_service import build_section_draft

        prior, *_ = _make_deal(db_session, test_user, company_name="Acme Electronics", category="resistors")
        prior.sales_testing_option = "Full functional"
        prior.sales_section_reviewed_at = datetime.now(UTC)
        db_session.commit()

        qp, *_ = _make_deal(db_session, test_user, company_name="Acme Electronics", category="capacitors")
        draft = build_section_draft(db_session, qp, "sales")
        assert "sales_testing_option" not in draft  # different commodity → no carry-forward

    def test_unreviewed_prior_qp_does_not_carry(self, db_session, test_user):
        from app.services.qp_draft_service import build_section_draft

        prior, *_ = _make_deal(db_session, test_user, company_name="Acme Electronics", category="capacitors")
        prior.sales_testing_option = "Full functional"  # NOT reviewed
        db_session.commit()

        qp, *_ = _make_deal(db_session, test_user, company_name="Acme Electronics", category="capacitors")
        draft = build_section_draft(db_session, qp, "sales")
        assert "sales_testing_option" not in draft

    def test_multiline_recent_prior_does_not_starve_older_prior(self, db_session, test_user):
        """A newer multi-line prior QP must not consume the whole 3-QP window (the
        carry-forward query must count distinct QPs, not fanned-out lines)."""
        from datetime import timedelta

        from app.services.qp_draft_service import build_section_draft

        # OLDER prior: single line, HAS the policy field, reviewed earlier.
        old, *_ = _make_deal(db_session, test_user, company_name="Acme Electronics", category="capacitors")
        old.sales_test_location = "TRIO Lab"
        old.sales_section_reviewed_at = datetime.now(UTC) - timedelta(days=2)
        db_session.commit()

        # NEWER prior: THREE capacitor lines, does NOT set the field, reviewed later.
        new, new_bp, new_rq, _ = _make_deal(
            db_session, test_user, company_name="Acme Electronics", category="capacitors"
        )
        for _ in range(2):  # +2 more capacitor lines → 3 total (would fan the old inner join to 3 rows)
            _MPN_SEQ[0] += 1
            mpn = f"LM{_MPN_SEQ[0]:04d}"
            card = MaterialCard(normalized_mpn=mpn, display_mpn=mpn, category="capacitors")
            db_session.add(card)
            db_session.flush()
            r = Requirement(requisition_id=new_bp.requisition_id, primary_mpn=mpn, material_card_id=card.id)
            db_session.add(r)
            db_session.flush()
            db_session.add(BuyPlanLine(buy_plan_id=new_bp.id, requirement_id=r.id, quantity=1))
        new.sales_section_reviewed_at = datetime.now(UTC) - timedelta(days=1)
        db_session.commit()

        qp, *_ = _make_deal(db_session, test_user, company_name="Acme Electronics", category="capacitors")
        draft = build_section_draft(db_session, qp, "sales")
        assert draft["sales_test_location"]["value"] == "TRIO Lab"  # older prior not starved by the 3-line newer QP


class TestPasteExtraction:
    async def test_extract_from_paste_returns_fields(self, db_session, test_user):
        from app.services.qp_draft_service import extract_qp_fields_from_paste

        with patch(
            "app.services.qp_draft_service.claude_structured",
            new_callable=AsyncMock,
            return_value={"sales_condition": "Factory New", "sales_testing_specifics": "Burn-in 48h"},
        ):
            out = await extract_qp_fields_from_paste("customer TSO text...", "sales")
        assert out["sales_condition"] == "Factory New"
        assert out["sales_testing_specifics"] == "Burn-in 48h"

    async def test_extract_drops_unknown_fields(self, db_session, test_user):
        from app.services.qp_draft_service import extract_qp_fields_from_paste

        with patch(
            "app.services.qp_draft_service.claude_structured",
            new_callable=AsyncMock,
            return_value={"sales_condition": "New", "not_a_field": "x", "purchasing_packaging": "wrong section"},
        ):
            out = await extract_qp_fields_from_paste("text", "sales")
        assert out == {"sales_condition": "New"}  # only whitelisted sales fields survive

    async def test_extract_empty_text_no_ai_call(self, db_session, test_user):
        from app.services.qp_draft_service import extract_qp_fields_from_paste

        with patch("app.services.qp_draft_service.claude_structured", new_callable=AsyncMock) as ai:
            out = await extract_qp_fields_from_paste("   ", "sales")
        assert out == {}
        ai.assert_not_called()


class TestDraftRoutes:
    def test_draft_route_prefills_but_writes_nothing(self, client, db_session, test_user):
        qp, *_ = _make_deal(db_session, test_user, category="capacitors")
        resp = client.post(f"/v2/qp/{qp.id}/draft/sales", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "capacitors" in resp.text  # commodity prefilled from the deal
        assert "Nothing is written yet" in resp.text  # amber review banner
        assert "Accept &amp; save" in resp.text
        # Suggest-only: the draft POST must NOT persist anything.
        db_session.expire_all()
        assert db_session.get(QualityPlan, qp.id).sales_condition is None

    def test_draft_route_locked_section_shows_no_draft(self, client, db_session, test_user):
        qp, *_ = _make_deal(db_session, test_user, category="capacitors")
        qp.sales_section_reviewed_at = datetime.now(UTC)
        db_session.commit()
        resp = client.post(f"/v2/qp/{qp.id}/draft/sales", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "Nothing is written yet" not in resp.text  # locked → read-only, no draft

    def test_draft_from_paste_route(self, client, db_session, test_user):
        qp, *_ = _make_deal(db_session, test_user, category="capacitors")
        qp.sales_condition = "Preset"  # so the deal deterministic doesn't fill it; paste will
        db_session.commit()
        with patch(
            "app.services.qp_draft_service.claude_structured",
            new_callable=AsyncMock,
            return_value={"sales_testing_specifics": "Burn-in 48h per customer TSO"},
        ):
            resp = client.post(
                f"/v2/qp/{qp.id}/draft/sales",
                data={"pasted_text": "customer TSO says burn-in 48h"},
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert "Burn-in 48h per customer TSO" in resp.text
        assert "AI — verify" in resp.text  # the AI source chip

    def test_discard_route_renders_clean(self, client, db_session, test_user):
        qp, *_ = _make_deal(db_session, test_user, category="capacitors")
        resp = client.get(f"/v2/qp/{qp.id}/section/sales", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "Nothing is written yet" not in resp.text  # clean render, no draft banner

    def test_draft_unknown_section_404(self, client, db_session, test_user):
        qp, *_ = _make_deal(db_session, test_user)
        resp = client.post(f"/v2/qp/{qp.id}/draft/serial", headers={"HX-Request": "true"})
        assert resp.status_code == 404
