"""tests/test_build_quote_multi_req.py — Bulk cross-req → ONE combined quote
(OQ-02/REQ-04).

Covers the combined-quote feature end to end:
  - saving spans every selected requisition into ONE quote (join rows, both/all QUOTED);
  - the customer-consistency gate blocks a modal-open (honest 200 fragment) AND a save
    (400 naming each offending req) — never a silent partial write;
  - the single-req path is 100% preserved (1 quote, 1 self join-row id == req id);
  - a SECONDARY requisition surfaces the combined quote on its Quotes tab, Build-Quote
    tab, and the list Quotes column;
  - send advances EVERY contributing requisition + logs one ActivityLog per req;
  - a revision carries the full requisition membership;
  - building a buy plan from a combined quote is hard-blocked (single-req still works).

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user, test_company,
            test_customer_site).
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import RequisitionStatus
from app.models import (
    ActivityLog,
    Company,
    CustomerSite,
    Offer,
    Quote,
    QuoteLine,
    QuoteRequisition,
    Requirement,
    Requisition,
    User,
)
from app.schemas.quote_builder import QuoteBuilderSaveRequest
from tests.conftest import engine

_ = engine


# ── Fixtures / helpers ─────────────────────────────────────────────────────────


def _make_req(db: Session, user: User, site_id: int, name: str, mpn: str, price: float):
    """Create a requisition on *site_id* with one requirement and one ACTIVE offer.

    Returns (requisition, requirement, offer).
    """
    req = Requisition(
        name=name,
        customer_name="Combo Co",
        status="open",
        customer_site_id=site_id,
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        manufacturer="TI",
        target_qty=100,
        condition="new",
        created_at=datetime.now(timezone.utc),
    )
    db.add(item)
    db.flush()
    offer = Offer(
        requisition_id=req.id,
        requirement_id=item.id,
        vendor_name="Avnet",
        mpn=mpn,
        normalized_mpn=mpn,
        status="active",
        unit_price=price,
        qty_available=500,
        entered_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(offer)
    db.commit()
    db.refresh(req)
    db.refresh(item)
    db.refresh(offer)
    return req, item, offer


@pytest.fixture()
def combo(db_session: Session, test_user: User, test_customer_site: CustomerSite):
    """Three requisitions sharing ONE customer site, each with a requirement + offer."""
    r1 = _make_req(db_session, test_user, test_customer_site.id, "COMBO-1", "LM317T", 0.40)
    r2 = _make_req(db_session, test_user, test_customer_site.id, "COMBO-2", "NE555P", 0.20)
    r3 = _make_req(db_session, test_user, test_customer_site.id, "COMBO-3", "LM358DR", 0.30)
    return {"r1": r1, "r2": r2, "r3": r3}


@pytest.fixture()
def other_site(db_session: Session) -> CustomerSite:
    """A customer site for a DIFFERENT company (to force a customer mismatch)."""
    co = Company(name="Different Customer Inc", created_at=datetime.now(timezone.utc))
    db_session.add(co)
    db_session.flush()
    site = CustomerSite(company_id=co.id, site_name="Different HQ", contact_email="buyer@different.com")
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


def _line(requirement, offer, mpn: str, sell: float, cost: float) -> dict:
    return {
        "requirement_id": requirement.id,
        "offer_id": offer.id,
        "mpn": mpn,
        "manufacturer": "TI",
        "qty": 100,
        "cost_price": cost,
        "sell_price": sell,
        "margin_pct": round((sell - cost) / sell * 100, 2),
        "condition": "new",
    }


def _multi_save(client: TestClient, req_ids: list[int], lines: list[dict], quote_id=None):
    ids = ",".join(str(i) for i in req_ids)
    return client.post(
        f"/v2/partials/quote-builder/multi/save?requisition_ids={ids}",
        json={"lines": lines, "quote_id": quote_id},
    )


# ── Save: one combined quote ────────────────────────────────────────────────────


class TestMultiSave:
    def test_multi_save_creates_one_combined_quote(self, client, db_session, combo):
        r1, i1, o1 = combo["r1"]
        r2, i2, o2 = combo["r2"]
        lines = [_line(i1, o1, "LM317T", 0.60, 0.40), _line(i2, o2, "NE555P", 0.30, 0.20)]

        resp = _multi_save(client, [r1.id, r2.id], lines)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True

        quotes = db_session.query(Quote).all()
        assert len(quotes) == 1
        quote = quotes[0]
        # ONE combined quote anchored on the primary (first selected).
        assert quote.requisition_id == r1.id
        # Two contributing join rows (primary + secondary).
        links = db_session.query(QuoteRequisition).filter(QuoteRequisition.quote_id == quote.id).all()
        assert {link.requisition_id for link in links} == {r1.id, r2.id}
        # QuoteLine count = lines from BOTH requisitions.
        qlines = db_session.query(QuoteLine).filter(QuoteLine.quote_id == quote.id).all()
        assert len(qlines) == 2
        # BOTH requisitions advanced to QUOTED.
        db_session.expire_all()
        assert db_session.get(Requisition, r1.id).status == RequisitionStatus.QUOTED
        assert db_session.get(Requisition, r2.id).status == RequisitionStatus.QUOTED

    def test_multi_save_three_reqs(self, client, db_session, combo):
        r1, i1, o1 = combo["r1"]
        r2, i2, o2 = combo["r2"]
        r3, i3, o3 = combo["r3"]
        lines = [
            _line(i1, o1, "LM317T", 0.60, 0.40),
            _line(i2, o2, "NE555P", 0.30, 0.20),
            _line(i3, o3, "LM358DR", 0.45, 0.30),
        ]
        resp = _multi_save(client, [r1.id, r2.id, r3.id], lines)
        assert resp.status_code == 200, resp.text
        quote = db_session.query(Quote).one()
        links = db_session.query(QuoteRequisition).filter(QuoteRequisition.quote_id == quote.id).all()
        assert {link.requisition_id for link in links} == {r1.id, r2.id, r3.id}
        assert db_session.query(QuoteLine).filter(QuoteLine.quote_id == quote.id).count() == 3

    def test_empty_selection_rejected(self, client, combo):
        r1, i1, o1 = combo["r1"]
        resp = client.post(
            "/v2/partials/quote-builder/multi/save?requisition_ids=",
            json={"lines": [_line(i1, o1, "LM317T", 0.60, 0.40)], "quote_id": None},
        )
        assert resp.status_code == 400


# ── Customer-consistency gate ───────────────────────────────────────────────────


class TestCustomerConsistency:
    def test_customer_mismatch_blocks_modal_open(self, client, db_session, test_user, test_customer_site, other_site):
        r1, _, _ = _make_req(db_session, test_user, test_customer_site.id, "MM-1", "LM317T", 0.40)
        r2, _, _ = _make_req(db_session, test_user, other_site.id, "MM-2", "NE555P", 0.20)

        resp = client.get(f"/v2/partials/quote-builder/multi?requisition_ids={r1.id},{r2.id}")
        # Honest 200 error fragment — NOT an exception/toast.
        assert resp.status_code == 200
        assert "different customers" in resp.text
        # The Alpine builder must NOT init against the error fragment.
        assert "quoteBuilder(" not in resp.text
        # No quote is created just by opening.
        assert db_session.query(Quote).count() == 0

    def test_customer_mismatch_blocks_save(self, client, db_session, test_user, test_customer_site, other_site):
        r1, i1, o1 = _make_req(db_session, test_user, test_customer_site.id, "MM-1", "LM317T", 0.40)
        r2, i2, o2 = _make_req(db_session, test_user, other_site.id, "MM-2", "NE555P", 0.20)
        lines = [_line(i1, o1, "LM317T", 0.60, 0.40), _line(i2, o2, "NE555P", 0.30, 0.20)]

        resp = _multi_save(client, [r1.id, r2.id], lines)
        assert resp.status_code == 400
        detail = resp.json().get("detail", "") or resp.json().get("error", "")
        # Detail names BOTH requisitions so the salesperson can fix the selection.
        assert "MM-1" in detail and "MM-2" in detail
        # Nothing was written — no partial quote, no join rows.
        assert db_session.query(Quote).count() == 0
        assert db_session.query(QuoteRequisition).count() == 0

    def test_missing_customer_site_blocks(self, client, db_session, test_user, test_customer_site):
        r1, i1, o1 = _make_req(db_session, test_user, test_customer_site.id, "MS-1", "LM317T", 0.40)
        # r2 has NO customer site.
        r2 = Requisition(name="MS-2", status="open", created_by=test_user.id, created_at=datetime.now(timezone.utc))
        db_session.add(r2)
        db_session.flush()
        i2 = Requirement(
            requisition_id=r2.id, primary_mpn="NE555P", target_qty=10, created_at=datetime.now(timezone.utc)
        )
        db_session.add(i2)
        db_session.flush()
        o2 = Offer(
            requisition_id=r2.id,
            requirement_id=i2.id,
            vendor_name="Avnet",
            mpn="NE555P",
            normalized_mpn="NE555P",
            status="active",
            unit_price=0.20,
            qty_available=10,
            entered_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(o2)
        db_session.commit()

        lines = [_line(i1, o1, "LM317T", 0.60, 0.40), _line(i2, o2, "NE555P", 0.30, 0.20)]
        resp = _multi_save(client, [r1.id, r2.id], lines)
        assert resp.status_code == 400
        detail = resp.json().get("detail", "") or resp.json().get("error", "")
        assert "MS-2" in detail
        assert db_session.query(Quote).count() == 0


# ── Single-req regression ───────────────────────────────────────────────────────


class TestSingleReqPreserved:
    def test_single_req_path_unaffected(self, db_session, test_user, combo):
        """save_quote_from_builder (single) still makes 1 quote + exactly 1 self join-
        row."""
        from app.services.quote_builder_service import save_quote_from_builder

        r1, i1, o1 = combo["r1"]
        payload = QuoteBuilderSaveRequest(lines=[_line(i1, o1, "LM317T", 0.60, 0.40)])
        result = save_quote_from_builder(db_session, req_id=r1.id, payload=payload, user=test_user)
        assert result["ok"] is True

        links = db_session.query(QuoteRequisition).filter(QuoteRequisition.quote_id == result["quote_id"]).all()
        assert len(links) == 1
        assert links[0].requisition_id == r1.id


# ── Secondary-requisition visibility ────────────────────────────────────────────


class TestSecondaryReqVisibility:
    def _build_combined(self, client, combo):
        r1, i1, o1 = combo["r1"]
        r2, i2, o2 = combo["r2"]
        lines = [_line(i1, o1, "LM317T", 0.60, 0.40), _line(i2, o2, "NE555P", 0.30, 0.20)]
        resp = _multi_save(client, [r1.id, r2.id], lines)
        assert resp.status_code == 200, resp.text
        return r1, r2, resp.json()["quote_number"]

    def test_secondary_req_quotes_tab_shows_combined(self, client, combo):
        _, r2, quote_number = self._build_combined(client, combo)
        resp = client.get(f"/v2/partials/requisitions/{r2.id}/tab/quotes")
        assert resp.status_code == 200
        # The combined quote shows on the SECONDARY req's Quotes tab (join-scoped read).
        assert quote_number in resp.text

    def test_secondary_req_build_quote_tab_shows_existing(self, client, combo):
        _, r2, quote_number = self._build_combined(client, combo)
        resp = client.get(f"/v2/partials/requisitions/{r2.id}/build-quote")
        assert resp.status_code == 200
        # The secondary req's Build-Quote tab surfaces the existing combined quote summary.
        assert quote_number in resp.text

    def test_list_quotes_column_reflects_combined_on_secondary(self, client, db_session, combo):
        _, r2, _ = self._build_combined(client, combo)
        # After sending, the quote is SENT — the list Quotes column shows it on the secondary.
        quote = db_session.query(Quote).one()
        quote.status = "sent"
        db_session.commit()
        resp = client.get("/v2/partials/requisitions")
        assert resp.status_code == 200
        from app.routers.htmx.requisitions import _best_quote_status
        from app.services.quote_requisitions import quotes_for_requisition

        # The join-based read surfaces the combined quote for the secondary requisition.
        assert _best_quote_status(quotes_for_requisition(db_session, r2.id).all()) == "sent"


# ── Send transitions every contributing req ─────────────────────────────────────


class TestSendAllReqs:
    async def test_send_transitions_all_contributing_reqs(self, db_session, test_user, combo):
        from app.services.quote_builder_service import save_quote_from_builder_multi
        from app.services.quote_send import send_quote_email

        r1, i1, o1 = combo["r1"]
        r2, i2, o2 = combo["r2"]
        lines = [_line(i1, o1, "LM317T", 0.60, 0.40), _line(i2, o2, "NE555P", 0.30, 0.20)]
        result = save_quote_from_builder_multi(db_session, [r1.id, r2.id], _payload(lines), test_user)
        quote = db_session.get(Quote, result["quote_id"])
        # Draft first so quote → SENT is a valid transition.
        quote.status = "draft"
        db_session.commit()
        send_subject = f"Quote {quote.quote_number} sent"

        # Use the REAL-send path (testing=False) with a patched Graph so graph_message_id is
        # NON-null — testing=True leaves it None, which disables log_email_activity's
        # external_id dedup and masks the bug. With a real id, a SHARED external_id would
        # silently drop every contributing req's send log after the primary; this asserts
        # each contributing req still records exactly one.
        with (
            patch("app.utils.graph_client.GraphClient.post_json", new=AsyncMock(return_value={})),
            patch(
                "app.email_service._find_sent_message",
                new=AsyncMock(return_value={"id": "MSG-QSEND-1", "conversationId": "CONV-QSEND-1"}),
            ),
        ):
            await send_quote_email(db_session, quote, test_user, token="t", testing=False)

        db_session.expire_all()
        assert db_session.get(Requisition, r1.id).status == RequisitionStatus.QUOTED
        assert db_session.get(Requisition, r2.id).status == RequisitionStatus.QUOTED
        # Exactly one outbound send ActivityLog per contributing requisition (scoped by the
        # send's own subject so status-transition activity rows don't inflate the count).
        for rid in (r1.id, r2.id):
            assert (
                db_session.query(ActivityLog)
                .filter(ActivityLog.requisition_id == rid, ActivityLog.subject == send_subject)
                .count()
                == 1
            )


# ── Revision carries membership ─────────────────────────────────────────────────


class TestReviseMembership:
    def test_revise_carries_multi_req_membership(self, client, db_session, combo):
        r1, i1, o1 = combo["r1"]
        r2, i2, o2 = combo["r2"]
        lines = [_line(i1, o1, "LM317T", 0.60, 0.40), _line(i2, o2, "NE555P", 0.30, 0.20)]
        assert _multi_save(client, [r1.id, r2.id], lines).status_code == 200
        parent = db_session.query(Quote).one()

        resp = client.post(f"/v2/partials/quotes/{parent.id}/revise")
        assert resp.status_code == 200
        db_session.expire_all()
        new_quote = db_session.query(Quote).filter(Quote.revision == 2).one()
        # The revision links the SAME contributing requisitions as its parent.
        links = db_session.query(QuoteRequisition).filter(QuoteRequisition.quote_id == new_quote.id).all()
        assert {link.requisition_id for link in links} == {r1.id, r2.id}


# ── Buy-plan hard guard ─────────────────────────────────────────────────────────


class TestBuyPlanGuard:
    def test_build_buy_plan_blocked_for_combined(self, db_session, test_user, combo):
        from app.services.buyplan_builder import build_buy_plan
        from app.services.quote_builder_service import save_quote_from_builder_multi

        r1, i1, o1 = combo["r1"]
        r2, i2, o2 = combo["r2"]
        lines = [_line(i1, o1, "LM317T", 0.60, 0.40), _line(i2, o2, "NE555P", 0.30, 0.20)]
        result = save_quote_from_builder_multi(db_session, [r1.id, r2.id], _payload(lines), test_user)

        with pytest.raises(ValueError, match="spans 2 requisitions"):
            build_buy_plan(result["quote_id"], db_session)
        # No buy plan was created.
        from app.models import BuyPlan

        assert db_session.query(BuyPlan).count() == 0

    def test_build_buy_plan_still_works_single_req(self, db_session, test_user, combo):
        from app.services.buyplan_builder import build_buy_plan
        from app.services.quote_builder_service import save_quote_from_builder

        r1, i1, o1 = combo["r1"]
        result = save_quote_from_builder(
            db_session, req_id=r1.id, payload=_payload([_line(i1, o1, "LM317T", 0.60, 0.40)]), user=test_user
        )
        quote = db_session.get(Quote, result["quote_id"])
        quote.status = "won"
        db_session.commit()

        plan = build_buy_plan(result["quote_id"], db_session)
        assert plan is not None
        assert plan.requisition_id == r1.id


def _payload(lines: list[dict]) -> QuoteBuilderSaveRequest:
    return QuoteBuilderSaveRequest(lines=lines)
