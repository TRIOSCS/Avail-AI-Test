"""test_resell_myday_followup.py — Resell "not yet this round" My-Day follow-up tasks.

Covers the CRM-Phase-2 seam wired into the resell not-yet strip: each buyer the
"usually offered, not yet this round" nudge surfaces also lands as a durable My-Day
follow-up RequisitionTask assigned to the LIST OWNER, so the nudge survives a page
close. Creation is idempotent per (excess list, buyer card, owner) — reloading the
strip never duplicates a buyer's task.

Two layers:
- service: task_service.auto_create_resell_followup_task (creation + idempotency,
  including across done tasks, separate buyers, and separate owners).
- route: GET /v2/partials/resell/{id}/not-yet-strip creates one task per surfaced
  buyer for the owner and is idempotent on reload (the strip ranking itself is
  stubbed — it has its own coverage in test_buyer_affinity_service.py).

Called by: pytest
Depends on: conftest.py (db_session, test_user, test_company, client),
            services.task_service, services.buyer_affinity_service.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

os.environ["TESTING"] = "1"

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus, TaskStatus
from app.models import Company, User, VendorCard
from app.models.excess import ExcessLineItem, ExcessList
from app.models.task import RequisitionTask
from app.services import task_service
from app.services.buyer_affinity_service import RankedBuyer
from app.utils.normalization import normalize_mpn_key

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_card(db: Session, name: str) -> VendorCard:
    card = VendorCard(
        normalized_name=name.lower(),
        display_name=name,
        emails=[f"sales@{name.lower().replace(' ', '')}.com"],
        created_at=datetime.now(UTC),
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


@pytest.fixture()
def buyer_card_a(db_session: Session) -> VendorCard:
    return _make_card(db_session, "Buyer Alpha")


@pytest.fixture()
def buyer_card_b(db_session: Session) -> VendorCard:
    return _make_card(db_session, "Buyer Beta")


@pytest.fixture()
def owned_list(db_session: Session, test_user: User, test_company: Company) -> ExcessList:
    """A collecting list owned by test_user (the client fixture's auth user)."""
    el = ExcessList(
        title="Acme surplus",
        company_id=test_company.id,
        owner_id=test_user.id,
        status=ExcessListStatus.COLLECTING,
        total_line_items=1,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.flush()
    db_session.add(
        ExcessLineItem(
            excess_list_id=el.id,
            part_number="XCVU9P-2FLGA2104I",
            normalized_part_number=normalize_mpn_key("XCVU9P-2FLGA2104I"),
            quantity=50,
            condition="New",
        )
    )
    db_session.commit()
    db_session.refresh(el)
    return el


def _tasks_for(db: Session, owner_id: int) -> list[RequisitionTask]:
    return db.query(RequisitionTask).filter(RequisitionTask.assigned_to_id == owner_id).all()


# ---------------------------------------------------------------------------
# Service layer — auto_create_resell_followup_task
# ---------------------------------------------------------------------------


class TestFollowupTaskService:
    def test_creates_followup_task_for_buyer(self, db_session, test_user, owned_list, buyer_card_a):
        task = task_service.auto_create_resell_followup_task(
            db_session,
            excess_list_id=owned_list.id,
            vendor_card_id=buyer_card_a.id,
            owner_id=test_user.id,
            buyer_name=buyer_card_a.display_name,
        )
        assert task is not None
        assert task.assigned_to_id == test_user.id
        assert task.vendor_card_id == buyer_card_a.id
        assert task.source == "system"
        assert task.source_ref == f"resell_notyet:{owned_list.id}:{buyer_card_a.id}"
        assert buyer_card_a.display_name in task.title
        # #12: this task is scoped to the buyer's vendor card and renders on the SHARED
        # cross-trader buyer Tasks tab, so the customer-named list title ("Acme surplus")
        # must NEVER appear in it — it references the list by the neutral id-derived label.
        assert owned_list.title not in task.title, "customer-named list title leaked into the cross-trader task title"
        assert f"Excess listing #{owned_list.id}" in task.title
        assert len(_tasks_for(db_session, test_user.id)) == 1

    def test_idempotent_same_buyer_list_owner(self, db_session, test_user, owned_list, buyer_card_a):
        for _ in range(3):
            task_service.auto_create_resell_followup_task(
                db_session,
                excess_list_id=owned_list.id,
                vendor_card_id=buyer_card_a.id,
                owner_id=test_user.id,
                buyer_name=buyer_card_a.display_name,
            )
        assert len(_tasks_for(db_session, test_user.id)) == 1

    def test_idempotent_even_after_done(self, db_session, test_user, owned_list, buyer_card_a):
        """A completed follow-up is not recreated on reload (don't re-nag a dismissed
        nudge)."""
        task = task_service.auto_create_resell_followup_task(
            db_session,
            excess_list_id=owned_list.id,
            vendor_card_id=buyer_card_a.id,
            owner_id=test_user.id,
            buyer_name=buyer_card_a.display_name,
        )
        task.status = TaskStatus.DONE.value
        task.completed_at = datetime.now(UTC)
        db_session.commit()
        task_service.auto_create_resell_followup_task(
            db_session,
            excess_list_id=owned_list.id,
            vendor_card_id=buyer_card_a.id,
            owner_id=test_user.id,
            buyer_name=buyer_card_a.display_name,
        )
        assert len(_tasks_for(db_session, test_user.id)) == 1

    def test_separate_buyers_get_separate_tasks(self, db_session, test_user, owned_list, buyer_card_a, buyer_card_b):
        for card in (buyer_card_a, buyer_card_b):
            task_service.auto_create_resell_followup_task(
                db_session,
                excess_list_id=owned_list.id,
                vendor_card_id=card.id,
                owner_id=test_user.id,
                buyer_name=card.display_name,
            )
        assert len(_tasks_for(db_session, test_user.id)) == 2

    def test_separate_owners_get_separate_tasks(self, db_session, test_user, manager_user, owned_list, buyer_card_a):
        for owner in (test_user, manager_user):
            task_service.auto_create_resell_followup_task(
                db_session,
                excess_list_id=owned_list.id,
                vendor_card_id=buyer_card_a.id,
                owner_id=owner.id,
                buyer_name=buyer_card_a.display_name,
            )
        assert len(_tasks_for(db_session, test_user.id)) == 1
        assert len(_tasks_for(db_session, manager_user.id)) == 1


# ---------------------------------------------------------------------------
# Route layer — GET /v2/partials/resell/{id}/not-yet-strip
# ---------------------------------------------------------------------------


def _stub_strip(monkeypatch, ranked: list[RankedBuyer]) -> None:
    """Pin the strip ranking to a fixed buyer set (ranking has its own coverage)."""
    monkeypatch.setattr(
        "app.services.buyer_affinity_service.not_yet_offered_strip",
        lambda db, *, excess_list_id, **kw: ranked,
    )


def _ranked(card: VendorCard) -> RankedBuyer:
    return RankedBuyer(
        vendor_card_id=card.id,
        display_name=card.display_name,
        last_bid=None,
        win_rate=None,
        last_offered_at=None,
        rank_reason="buys_this_commodity",
    )


class TestNotYetStripRoute:
    def test_route_creates_one_task_per_buyer(
        self, client, db_session, monkeypatch, test_user, owned_list, buyer_card_a, buyer_card_b
    ):
        _stub_strip(monkeypatch, [_ranked(buyer_card_a), _ranked(buyer_card_b)])
        resp = client.get(f"/v2/partials/resell/{owned_list.id}/not-yet-strip")
        assert resp.status_code == 200
        tasks = _tasks_for(db_session, test_user.id)
        assert len(tasks) == 2
        scoped = {t.vendor_card_id for t in tasks}
        assert scoped == {buyer_card_a.id, buyer_card_b.id}
        assert all(t.assigned_to_id == test_user.id for t in tasks)

    def test_route_idempotent_on_reload(
        self, client, db_session, monkeypatch, test_user, owned_list, buyer_card_a, buyer_card_b
    ):
        _stub_strip(monkeypatch, [_ranked(buyer_card_a), _ranked(buyer_card_b)])
        client.get(f"/v2/partials/resell/{owned_list.id}/not-yet-strip")
        client.get(f"/v2/partials/resell/{owned_list.id}/not-yet-strip")
        assert len(_tasks_for(db_session, test_user.id)) == 2

    def test_route_no_buyers_creates_no_tasks(self, client, db_session, monkeypatch, test_user, owned_list):
        _stub_strip(monkeypatch, [])
        resp = client.get(f"/v2/partials/resell/{owned_list.id}/not-yet-strip")
        assert resp.status_code == 200
        assert _tasks_for(db_session, test_user.id) == []

    def test_chip_carries_preselect_buyer(self, client, db_session, monkeypatch, test_user, owned_list, buyer_card_a):
        """RS-8: each nudge chip opens the offer panel with its buyer preselected.

        The chip's hx-get must carry preselect_vendor_card_id=<buyer> so the panel lands
        with that buyer already checked (the one-click promise), not the generic panel.
        """
        _stub_strip(monkeypatch, [_ranked(buyer_card_a)])
        resp = client.get(f"/v2/partials/resell/{owned_list.id}/not-yet-strip")
        assert resp.status_code == 200
        assert f"preselect_vendor_card_id={buyer_card_a.id}" in resp.text
