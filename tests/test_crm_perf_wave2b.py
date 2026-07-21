"""tests/test_crm_perf_wave2b.py — result-equivalence guards for the CRM query-perf Wave
2b rewrites.

Each rewrite reduces N+1 / O(n^2) / unbounded-load / per-row work WITHOUT changing
results. These tests pin the observable output so a future regression (or a subtly
wrong optimization) is caught:

  1. quote_to_dict(cards=...) + list_quotes batch-load — identical card enrichment
     across multiple quotes, with a single materials query (N+1 killed).
  2. _latest_contact_notes — newest note per contact via a window function, incl. the
     empty-string fall-through and the id tie-break.
  3. company_edit_form — parent-company dropdown lists active companies (id+name) and
     excludes inactive + self.
  4. InboundCustomerSource.new_items_for_user — column-projected markers identical to
     the full-entity hydration.
  5. _render_company_detail — matched-requisition counts (open/quote/buy-plan) unchanged
     when the match is computed once and reused; helper req_ids= path == recompute path.
  6. company_dup_suggestion — SQLite (unchanged rapidfuzz) path still renders. The
     Postgres trgm branch is dialect-gated, so SQLite exercises the old path here; the
     PG path needs a live-PG parity check (run by the coordinator).
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import event, or_
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from app.constants import Channel, Direction, RequisitionStatus
from app.models import MaterialCard, Quote
from app.models.buy_plan import BuyPlan
from app.models.crm import Company, CustomerSite, SiteContact
from app.models.intelligence import ActivityLog
from app.models.sourcing import Requisition
from app.routers.crm import quote_to_dict
from app.routers.htmx.companies.detail import _company_buy_plans_query, _company_quotes_query
from app.services.crm_service import _latest_contact_notes

# ── shared helpers ──────────────────────────────────────────────────────────────


@contextmanager
def _count_sql(db: Session, needle: str):
    """Count executed statements whose (lowercased) SQL contains ``needle``."""
    engine = db.get_bind()
    hits: list[str] = []

    def _after(conn, cursor, statement, parameters, context, executemany):
        if needle.lower() in statement.lower():
            hits.append(statement)

    event.listen(engine, "after_cursor_execute", _after)
    try:
        yield hits
    finally:
        event.remove(engine, "after_cursor_execute", _after)


def _card(db: Session, mpn: str, desc: str, cat: str = "voltage_regulators") -> MaterialCard:
    mc = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        manufacturer="Texas Instruments",
        description=desc,
        category=cat,
    )
    db.add(mc)
    db.commit()
    db.refresh(mc)
    return mc


def _quote(db, req, site, user, number, revision, items) -> Quote:
    q = Quote(
        requisition_id=req.id,
        customer_site_id=site.id if site else None,
        quote_number=number,
        revision=revision,
        status="draft",
        line_items=items,
        subtotal=0,
        created_by_id=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


# ══════════════════════════════════════════════════════════════════════════════
#  1. quote_to_dict(cards=...) + list_quotes batch enrichment (N+1 kill)
# ══════════════════════════════════════════════════════════════════════════════


class TestQuoteToDictCardsMap:
    def test_prefetched_map_matches_db_path(self, db_session, test_requisition, test_customer_site, test_user):
        """quote_to_dict(cards=map) yields byte-identical line_items to
        quote_to_dict(db=db)."""
        a = _card(db_session, "LM317T", "Adjustable Voltage Regulator, 1.5A")
        b = _card(db_session, "NE555P", "Precision Timer IC")
        items = [
            {"mpn": "LM317T", "material_card_id": a.id, "qty": 100},
            {"mpn": "NE555P", "material_card_id": b.id, "qty": 5},
            {"mpn": "UNKNOWN", "qty": 1},  # no card id — untouched in both paths
        ]
        q = _quote(db_session, test_requisition, test_customer_site, test_user, "Q-W2B-DICT-1", 1, items)

        via_db = quote_to_dict(q, db=db_session)["line_items"]
        via_map = quote_to_dict(q, cards={a.id: a, b.id: b})["line_items"]
        assert via_map == via_db
        # sanity: enrichment actually happened
        assert via_map[0]["description"] == "Adjustable Voltage Regulator, 1.5A"
        assert via_map[1]["description"] == "Precision Timer IC"
        assert "description" not in via_map[2]

    def test_default_none_preserves_raw_behavior(self, db_session, test_requisition, test_customer_site, test_user):
        """Neither db nor cards supplied -> raw line_items (unchanged single-arg
        behavior)."""
        a = _card(db_session, "LM7805", "5V Linear Regulator")
        items = [{"mpn": "LM7805", "material_card_id": a.id}]
        q = _quote(db_session, test_requisition, test_customer_site, test_user, "Q-W2B-DICT-2", 1, items)
        d = quote_to_dict(q)  # no db, no cards
        assert d["line_items"] == items
        assert "description" not in d["line_items"][0]

    def test_empty_cards_map_degrades_to_raw(self, db_session, test_requisition, test_customer_site, test_user):
        """Cards={} (batch found no cards) -> items returned without enrichment, like a
        miss."""
        a = _card(db_session, "TL072", "JFET Op-Amp")
        items = [{"mpn": "TL072", "material_card_id": a.id}]
        q = _quote(db_session, test_requisition, test_customer_site, test_user, "Q-W2B-DICT-3", 1, items)
        d = quote_to_dict(q, cards={})
        assert d["line_items"][0]["mpn"] == "TL072"
        assert "description" not in d["line_items"][0]


class TestListQuotesBatch:
    def test_multiple_quotes_identical_card_data_single_query(
        self, client, db_session, test_requisition, test_customer_site, test_user
    ):
        """GET .../quotes enriches every quote's items identically to the per-quote db
        path, using ONE materials query (was one per card-bearing quote)."""
        a = _card(db_session, "LM317T", "Adjustable Voltage Regulator, 1.5A")
        b = _card(db_session, "NE555P", "Precision Timer IC")
        # rev2 references BOTH cards; rev1 references only A (overlap) + a dead card id.
        q_rev2 = _quote(
            db_session,
            test_requisition,
            test_customer_site,
            test_user,
            "Q-W2B-LIST-R2",
            2,
            [
                {"mpn": "LM317T", "material_card_id": a.id},
                {"mpn": "NE555P", "material_card_id": b.id},
            ],
        )
        q_rev1 = _quote(
            db_session,
            test_requisition,
            test_customer_site,
            test_user,
            "Q-W2B-LIST-R1",
            1,
            [
                {"mpn": "LM317T", "material_card_id": a.id},
                {"mpn": "GONE", "material_card_id": 999_999},  # nonexistent card -> no enrichment
            ],
        )

        # Oracle: the per-quote db path, in the endpoint's revision-desc order.
        oracle = [quote_to_dict(q, db=db_session)["line_items"] for q in (q_rev2, q_rev1)]

        with _count_sql(db_session, "from material_cards") as hits:
            resp = client.get(f"/api/requisitions/{test_requisition.id}/quotes")
        assert resp.status_code == 200
        got = [row["line_items"] for row in resp.json()]

        assert got == oracle
        # rev2: both cards enriched
        assert got[0][0]["description"] == "Adjustable Voltage Regulator, 1.5A"
        assert got[0][1]["description"] == "Precision Timer IC"
        # rev1: card A enriched, dead card id left raw
        assert got[1][0]["description"] == "Adjustable Voltage Regulator, 1.5A"
        assert "description" not in got[1][1]
        # N+1 killed: exactly one materials query for the whole list.
        assert len(hits) == 1

    def test_empty_quotes_no_materials_query(self, client, db_session, test_requisition):
        """A requisition with no quotes returns [] and issues no materials query."""
        with _count_sql(db_session, "from material_cards") as hits:
            resp = client.get(f"/api/requisitions/{test_requisition.id}/quotes")
        assert resp.status_code == 200
        assert resp.json() == []
        assert len(hits) == 0


# ══════════════════════════════════════════════════════════════════════════════
#  2. _latest_contact_notes — newest note per contact (window function)
# ══════════════════════════════════════════════════════════════════════════════


class TestLatestContactNotes:
    @staticmethod
    def _contact(db, site, name) -> SiteContact:
        c = SiteContact(customer_site_id=site.id, full_name=name, is_active=True)
        db.add(c)
        db.commit()
        db.refresh(c)
        return c

    @staticmethod
    def _note(db, contact, text, created_at) -> ActivityLog:
        a = ActivityLog(
            site_contact_id=contact.id,
            activity_type="note",
            channel=Channel.EMAIL,  # NOT NULL col; _latest_contact_notes ignores channel
            notes=text,
            created_at=created_at,
            occurred_at=created_at,
        )
        db.add(a)
        db.commit()
        db.refresh(a)
        return a

    def test_newest_per_contact_and_edge_cases(self, db_session, test_customer_site):
        base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

        # c1: three distinct-timestamp notes -> newest wins.
        c1 = self._contact(db_session, test_customer_site, "Alpha")
        self._note(db_session, c1, "oldest", base)
        self._note(db_session, c1, "middle", base + timedelta(hours=1))
        self._note(db_session, c1, "newest", base + timedelta(hours=2))

        # c2: newest note is "" -> falls through to the older NON-empty note.
        c2 = self._contact(db_session, test_customer_site, "Bravo")
        self._note(db_session, c2, "real note", base)
        self._note(db_session, c2, "", base + timedelta(hours=1))

        # c3: only NULL / "" notes -> absent from the result dict.
        c3 = self._contact(db_session, test_customer_site, "Charlie")
        self._note(db_session, c3, None, base)
        self._note(db_session, c3, "", base + timedelta(hours=1))

        # c4: two notes at the SAME timestamp -> deterministic id-DESC tie-break wins.
        c4 = self._contact(db_session, test_customer_site, "Delta")
        tie = base + timedelta(hours=3)
        low = self._note(db_session, c4, "tie-low-id", tie)
        high = self._note(db_session, c4, "tie-high-id", tie)
        assert high.id > low.id

        result = _latest_contact_notes(db_session, [c1.id, c2.id, c3.id, c4.id])

        assert result[c1.id] == "newest"
        assert result[c2.id] == "real note"
        assert c3.id not in result
        assert result[c4.id] == "tie-high-id"
        # one row per qualifying contact (c1,c2,c4) — never the full note history.
        assert set(result) == {c1.id, c2.id, c4.id}

    def test_empty_contact_ids_short_circuits(self, db_session):
        assert _latest_contact_notes(db_session, []) == {}


# ══════════════════════════════════════════════════════════════════════════════
#  3. company_edit_form — parent-company dropdown (id/name projection)
# ══════════════════════════════════════════════════════════════════════════════


class TestCompanyEditFormDropdown:
    def test_dropdown_lists_active_excludes_inactive_and_self(self, client, db_session, test_user):
        edited = Company(
            name="Edited Co W2B", is_active=True, account_owner_id=test_user.id, created_at=datetime.now(UTC)
        )
        active_1 = Company(name="Active One W2B", is_active=True, created_at=datetime.now(UTC))
        active_2 = Company(name="Active Two W2B", is_active=True, created_at=datetime.now(UTC))
        inactive = Company(name="Inactive Co W2B", is_active=False, created_at=datetime.now(UTC))
        db_session.add_all([edited, active_1, active_2, inactive])
        db_session.commit()
        for c in (edited, active_1, active_2, inactive):
            db_session.refresh(c)

        resp = client.get(
            f"/v2/partials/customers/{edited.id}/edit-form",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # Scope assertions to the parent-company <select> — company ids can collide with
        # the account-owner (users) dropdown's option values elsewhere in the form.
        m = re.search(r'name="parent_company_id".*?</select>', resp.text, re.S)
        assert m is not None
        select_html = m.group(0)
        # Active peers present as <option value=ID>Name</option>
        assert f'value="{active_1.id}"' in select_html
        assert "Active One W2B" in select_html
        assert f'value="{active_2.id}"' in select_html
        assert "Active Two W2B" in select_html
        # Inactive excluded, and the edited company is not listed among its own parents.
        assert f'value="{inactive.id}"' not in select_html
        assert "Inactive Co W2B" not in select_html
        assert f'value="{edited.id}"' not in select_html


# ══════════════════════════════════════════════════════════════════════════════
#  4. InboundCustomerSource.new_items_for_user — projected markers == full-entity
# ══════════════════════════════════════════════════════════════════════════════


class TestInboundMarkersProjection:
    @staticmethod
    def _inbound(db, company, when) -> ActivityLog:
        a = ActivityLog(
            activity_type="email_received",
            channel=Channel.EMAIL,
            direction=Direction.INBOUND,
            company_id=company.id,
            subject="Re: pricing",
            occurred_at=when,
            created_at=when,
        )
        db.add(a)
        db.commit()
        db.refresh(a)
        return a

    def test_projected_markers_equal_full_entity_markers(self, db_session, test_user, test_company):
        from app.services.alerts.base import AlertItem
        from app.services.alerts.sources.inbound_customer import InboundCustomerSource

        # Own two Customer-type accounts as the rep.
        test_company.account_type = "Customer"
        test_company.account_owner_id = test_user.id
        second = Company(
            name="Second Customer W2B",
            account_type="Customer",
            account_owner_id=test_user.id,
            is_active=True,
            created_at=datetime.now(UTC),
        )
        db_session.add(second)
        db_session.commit()
        db_session.refresh(second)

        now = datetime.now(UTC)
        self._inbound(db_session, test_company, now - timedelta(hours=2))
        self._inbound(db_session, test_company, now - timedelta(hours=1))
        self._inbound(db_session, second, now - timedelta(minutes=30))

        source = InboundCustomerSource()
        # Oracle = the pre-rewrite full-entity hydration path, reconstructed inline.
        oracle = [
            AlertItem(ref_id=a.id, anchor=f"company-{a.company_id}")
            for a in source._eligible_query(db_session, test_user).all()
        ]
        got = source.new_items_for_user(db_session, test_user)

        assert [(i.ref_id, i.anchor) for i in got] == [(i.ref_id, i.anchor) for i in oracle]
        assert len(got) == 3
        # ordering preserved (oldest-first) and anchors well-formed
        assert got[0].anchor == f"company-{test_company.id}"
        assert got[-1].anchor == f"company-{second.id}"


# ══════════════════════════════════════════════════════════════════════════════
#  5. _render_company_detail — matched-requisition counts computed once & reused
# ══════════════════════════════════════════════════════════════════════════════


class TestRenderCompanyDetailMatchedReqs:
    @staticmethod
    def _req(db, user, *, name="REQ", customer_name=None, company_id=None, status="open") -> Requisition:
        r = Requisition(
            name=name,
            customer_name=customer_name,
            company_id=company_id,
            status=status,
            created_by=user.id,
            created_at=datetime.now(UTC),
        )
        db.add(r)
        db.commit()
        db.refresh(r)
        return r

    def _seed_account(self, db, user):
        company = Company(name="Matcher Co W2B", is_active=True, account_owner_id=user.id, created_at=datetime.now(UTC))
        db.add(company)
        db.commit()
        db.refresh(company)

        # matches by company_id FK (open)
        self._req(db, user, name="R-fk-open", company_id=company.id, status="open")
        # matches by exact customer_name (draft — draft counts as open-ish)
        self._req(db, user, name="R-name-draft", customer_name="Matcher Co W2B", status="draft")
        # matches by mixed-case / whitespace customer_name (open)
        self._req(db, user, name="R-name-ws", customer_name="  MATCHER co w2b  ", status="open")
        # matches but WON -> in the id set, NOT open
        self._req(db, user, name="R-won", company_id=company.id, status="won")
        # matches but NULL status -> in id set, NOT open (forced NULL below)
        r_null = self._req(db, user, name="R-null", company_id=company.id, status="open")
        db.execute(Requisition.__table__.update().where(Requisition.id == r_null.id).values(status=None))
        db.commit()
        # NON-matching (different company, different name)
        self._req(db, user, name="R-other", customer_name="Totally Different Inc", status="open")
        return company

    def test_helper_reqids_path_equals_recompute(self, db_session, test_user):
        company = self._seed_account(db_session, test_user)
        # Independent req-id oracle (the same predicate the helpers use internally).
        ids = [
            r.id
            for r in db_session.query(Requisition.id)
            .filter(
                or_(
                    Requisition.company_id == company.id,
                    sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
                )
            )
            .all()
        ]
        # Seed one quote + one buy plan so the counts are non-trivial.
        site = CustomerSite(company_id=company.id, site_name="HQ")
        db_session.add(site)
        db_session.commit()
        db_session.refresh(site)
        req_for_quote = db_session.query(Requisition).filter(Requisition.name == "R-fk-open").first()
        q = _quote(db_session, req_for_quote, site, test_user, "Q-W2B-DET-1", 1, [])
        bp = BuyPlan(quote_id=q.id, requisition_id=req_for_quote.id, status="draft", so_status="pending")
        db_session.add(bp)
        db_session.commit()

        cq_default = _company_quotes_query(db_session, company)
        cq_passed = _company_quotes_query(db_session, company, req_ids=ids)
        assert cq_default.count() == cq_passed.count()
        assert {x.id for x in cq_default} == {x.id for x in cq_passed}

        bpq_default = _company_buy_plans_query(db_session, company)
        bpq_passed = _company_buy_plans_query(db_session, company, req_ids=ids)
        assert bpq_default.count() == bpq_passed.count()
        assert {x.id for x in bpq_default} == {x.id for x in bpq_passed}

        # Empty req_ids -> buy-plans short-circuit to None (buy plans link only via reqs).
        assert _company_buy_plans_query(db_session, company, req_ids=[]) is None
        # Quotes with empty req_ids still return the SITE-linked query (company has a site),
        # so it is NOT None — the None case requires no sites AND no reqs:
        bare = Company(name="Bare Co W2B", is_active=True, created_at=datetime.now(UTC))
        db_session.add(bare)
        db_session.commit()
        db_session.refresh(bare)
        assert _company_quotes_query(db_session, bare, req_ids=[]) is None
        assert _company_buy_plans_query(db_session, bare, req_ids=[]) is None

    def test_render_counts_match_oracle(self, client, db_session, test_user, monkeypatch):
        from fastapi.responses import HTMLResponse

        import app.routers.htmx.companies.detail as detail_mod

        company = self._seed_account(db_session, test_user)
        site = CustomerSite(company_id=company.id, site_name="HQ")
        db_session.add(site)
        db_session.commit()
        db_session.refresh(site)
        req_for_quote = db_session.query(Requisition).filter(Requisition.name == "R-fk-open").first()
        q = _quote(db_session, req_for_quote, site, test_user, "Q-W2B-DET-2", 1, [])
        db_session.add(BuyPlan(quote_id=q.id, requisition_id=req_for_quote.id, status="draft", so_status="pending"))
        db_session.commit()

        # Oracle: the PRE-rewrite open-req COUNT (SQL status IN (open, draft)).
        oracle_open = (
            db_session.query(sqlfunc.count(Requisition.id))
            .filter(
                or_(
                    Requisition.company_id == company.id,
                    sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
                ),
                Requisition.status.in_([RequisitionStatus.OPEN, RequisitionStatus.DRAFT]),
            )
            .scalar()
            or 0
        )
        oracle_quotes = _company_quotes_query(db_session, company).count()
        oracle_buy = _company_buy_plans_query(db_session, company).count()
        assert oracle_open == 3  # fk-open + name-draft + name-ws (won/null/other excluded)

        captured: dict = {}

        def _capture(template_name, context, *a, **k):
            captured.update(context)
            return HTMLResponse("ok")

        monkeypatch.setattr(detail_mod, "template_response", _capture)

        resp = client.get(f"/v2/partials/customers/{company.id}", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert captured["open_req_count"] == oracle_open
        assert captured["quote_count"] == oracle_quotes
        assert captured["buy_plan_count"] == oracle_buy


# ══════════════════════════════════════════════════════════════════════════════
#  6. company_dup_suggestion — SQLite (unchanged rapidfuzz) path still renders
# ══════════════════════════════════════════════════════════════════════════════


class TestDupSuggestionSqlite:
    """SQLite has no pg_trgm, so the dialect gate keeps the ORIGINAL rapidfuzz path.

    These assert the endpoint still renders; the Postgres func.similarity/% branch is
    exercised separately by a live-PG parity check (run by the coordinator).
    """

    def test_no_near_dup_renders_empty(self, client, db_session, test_user):
        solo = Company(
            name="Unique Solo Co W2B", is_active=True, account_owner_id=test_user.id, created_at=datetime.now(UTC)
        )
        db_session.add(solo)
        db_session.commit()
        db_session.refresh(solo)
        resp = client.get(
            f"/v2/partials/customers/{solo.id}/dup-suggestion",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert resp.text == ""

    def test_near_dup_renders_banner(self, client, db_session, test_user):
        keeper = Company(
            name="Contoso Widgets Inc", is_active=True, account_owner_id=test_user.id, created_at=datetime.now(UTC)
        )
        dup = Company(
            name="Contoso Widgets LLC", is_active=True, account_owner_id=test_user.id, created_at=datetime.now(UTC)
        )
        db_session.add_all([keeper, dup])
        db_session.commit()
        db_session.refresh(keeper)
        resp = client.get(
            f"/v2/partials/customers/{keeper.id}/dup-suggestion",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Possible duplicate account" in resp.text
