"""test_ask_avail.py — TDD tests for Ask AVAIL (survey idea #18).

Natural-language questions dispatched to named, parameterized query templates
(NEVER free SQL): Claude maps the question to one template name + params, the
service coerces params and runs the hand-written ORM query, and the Enter-key
search path renders a uniform result table + one-tap CSV. Falls through to the
existing entity search when no template matches.

Covers: the dispatch (intent→template, param coercion, no-match/failure →
matched False), restricted-role read-gating on req-scoped templates, a
smoke-run of EVERY registered template (catches field typos), and the route
(renders the ask table on a match, falls through otherwise, throttled; the CSV
endpoint streams the same template deterministically).

Called by: pytest (TESTING=1 PYTHONPATH=. pytest tests/test_ask_avail.py -v)
Depends on: app.services.ask_avail_service, app.routers.htmx.search_views, conftest.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from app.models.intelligence import MaterialCard
from app.models.sourcing import Requisition

_HX = {"HX-Request": "true"}


def _req(db, user, *, name, customer="AcmeCo", status="open", deadline=None, created=None):
    r = Requisition(
        name=name,
        customer_name=customer,
        status=status,
        created_by=user.id,
        deadline=deadline,
        created_at=created or datetime.now(UTC),
    )
    db.add(r)
    db.flush()
    return r


# ── Service dispatch ───────────────────────────────────────────────────────────


class TestDispatch:
    async def test_matched_template_runs_and_returns_table(self, db_session, test_user):
        from app.services.ask_avail_service import answer_question

        _req(db_session, test_user, name="OPEN-1", customer="AcmeCo")
        db_session.commit()
        intent = {"template": "open_requisitions", "params": {"customer": "Acme"}}
        with patch("app.services.ask_avail_service.claude_structured", new_callable=AsyncMock, return_value=intent):
            out = await answer_question(db_session, test_user, "open reqs for Acme")
        assert out["matched"] is True
        assert out["template"] == "open_requisitions"
        assert out["params"] == {"customer": "Acme"}
        assert isinstance(out["columns"], list) and out["columns"]
        assert any("OPEN-1" in str(v) for row in out["rows"] for v in row.values())

    async def test_unknown_template_returns_not_matched(self, db_session, test_user):
        from app.services.ask_avail_service import answer_question

        intent = {"template": "none", "params": {}}
        with patch("app.services.ask_avail_service.claude_structured", new_callable=AsyncMock, return_value=intent):
            out = await answer_question(db_session, test_user, "what's the weather")
        assert out["matched"] is False

    async def test_ai_failure_returns_not_matched(self, db_session, test_user):
        from app.services.ask_avail_service import answer_question

        with patch("app.services.ask_avail_service.claude_structured", new_callable=AsyncMock, return_value=None):
            out = await answer_question(db_session, test_user, "anything")
        assert out["matched"] is False

    async def test_bad_template_name_from_ai_returns_not_matched(self, db_session, test_user):
        from app.services.ask_avail_service import answer_question

        intent = {"template": "'; DROP TABLE requisitions; --", "params": {}}
        with patch("app.services.ask_avail_service.claude_structured", new_callable=AsyncMock, return_value=intent):
            out = await answer_question(db_session, test_user, "evil")
        assert out["matched"] is False

    async def test_param_coercion_bounds_limit(self, db_session, test_user):
        from app.services.ask_avail_service import answer_question

        for i in range(3):
            db_session.add(MaterialCard(normalized_mpn=f"p{i}", display_mpn=f"P{i}", search_count=10 - i))
        db_session.commit()
        intent = {"template": "top_searched_parts", "params": {"limit": 999999}}
        with patch("app.services.ask_avail_service.claude_structured", new_callable=AsyncMock, return_value=intent):
            out = await answer_question(db_session, test_user, "most searched parts")
        assert out["matched"] is True
        assert len(out["rows"]) <= 100  # hard cap, not 999999


class TestReadGating:
    async def test_restricted_role_sees_only_owned_reqs(self, db_session, test_user, sales_user):
        from app.services.ask_avail_service import answer_question

        _req(db_session, test_user, name="OWNER-REQ", customer="Acme")  # owned by test_user
        r2 = Requisition(
            name="SALES-REQ",
            customer_name="Acme",
            status="open",
            created_by=sales_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(r2)
        db_session.commit()
        intent = {"template": "open_requisitions", "params": {}}
        with patch("app.services.ask_avail_service.claude_structured", new_callable=AsyncMock, return_value=intent):
            out = await answer_question(db_session, sales_user, "open reqs")
        blob = str(out["rows"])
        assert "SALES-REQ" in blob
        assert "OWNER-REQ" not in blob  # restricted role never sees another user's req


class TestEveryTemplateSmokeRuns:
    async def test_all_registered_templates_execute(self, db_session, test_user):
        """Every registered template runs without error on an empty-ish DB and returns
        (columns, rows) — catches field/enum typos across all 13."""
        from app.services.ask_avail_service import TEMPLATES, answer_question

        assert len(TEMPLATES) >= 13
        for name in TEMPLATES:
            intent = {"template": name, "params": {}}
            with patch("app.services.ask_avail_service.claude_structured", new_callable=AsyncMock, return_value=intent):
                out = await answer_question(db_session, test_user, f"run {name}")
            assert out["matched"] is True, name
            assert isinstance(out["columns"], list) and out["columns"], name
            assert isinstance(out["rows"], list), name


# ── Route ──────────────────────────────────────────────────────────────────────


class TestRoute:
    def test_enter_key_renders_ask_table_on_match(self, client, db_session, test_user):
        _req(db_session, test_user, name="ROUTE-OPEN-1")
        db_session.commit()
        intent = {"template": "open_requisitions", "params": {}}
        with patch("app.services.ask_avail_service.claude_structured", new_callable=AsyncMock, return_value=intent):
            resp = client.post("/v2/partials/search/ai", data={"q": "list open requisitions"}, headers=_HX)
        assert resp.status_code == 200
        body = resp.text
        assert "ROUTE-OPEN-1" in body
        assert "open_requisitions" in body  # transparency: which template ran is shown
        assert "ask.csv" in body  # one-tap CSV affordance

    def test_no_match_falls_through_to_entity_search(self, client, db_session, test_user):
        intent = {"template": "none", "params": {}}
        entity = {"best_match": None, "groups": {}, "total_count": 0}
        with (
            patch("app.services.ask_avail_service.claude_structured", new_callable=AsyncMock, return_value=intent),
            patch("app.services.global_search_service.ai_search", new_callable=AsyncMock, return_value=entity),
        ):
            resp = client.post("/v2/partials/search/ai", data={"q": "arrow electronics"}, headers=_HX)
        assert resp.status_code == 200
        # Entity-search render, not the ask table.
        assert "ask.csv" not in resp.text

    def test_ask_throttled_falls_through(self, client, db_session, test_user):
        entity = {"best_match": None, "groups": {}, "total_count": 0}
        with (
            patch("app.routers.htmx.search_views.check_rate_limit", return_value=False),
            patch("app.services.ask_avail_service.answer_question", new_callable=AsyncMock) as mock,
            patch("app.services.global_search_service.ai_search", new_callable=AsyncMock, return_value=entity),
        ):
            resp = client.post("/v2/partials/search/ai", data={"q": "open reqs"}, headers=_HX)
        assert resp.status_code == 200
        mock.assert_not_called()  # throttle skips the AI dispatch entirely
        # …and the response is the entity-search render (fall-through), not an ask table.
        assert "ask.csv" not in resp.text

    def test_csv_endpoint_streams_same_template(self, client, db_session, test_user):
        _req(db_session, test_user, name="CSV-OPEN-1")
        db_session.commit()
        resp = client.get(
            "/v2/partials/search/ask.csv", params={"template": "open_requisitions", "params": "{}"}, headers=_HX
        )
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")
        assert "CSV-OPEN-1" in resp.text

    def test_csv_endpoint_rejects_unknown_template(self, client, db_session, test_user):
        resp = client.get("/v2/partials/search/ask.csv", params={"template": "evil", "params": "{}"}, headers=_HX)
        assert resp.status_code == 404

    def test_csv_endpoint_unauthenticated_401(self, unauthenticated_client):
        resp = unauthenticated_client.get(
            "/v2/partials/search/ask.csv", params={"template": "open_requisitions", "params": "{}"}
        )
        assert resp.status_code == 401


# ── Adversarial-review fixes (7 confirmed findings) ────────────────────────────


class TestReviewFixes:
    async def test_past_deadline_uses_iso_string_compare(self, db_session, test_user):
        """Deadline is String(50) — the filter must compare against an ISO date string,
        not a datetime (PG rejects varchar < timestamp; sqlite masks it).

        A past ISO deadline matches; 'ASAP' and a future date do not.
        """
        from app.services.ask_avail_service import answer_question

        _req(db_session, test_user, name="PAST", customer="Acme")
        db_session.query(Requisition).filter_by(name="PAST").update({"deadline": "2000-01-01"})
        _req(db_session, test_user, name="ASAP-REQ", customer="Acme")
        db_session.query(Requisition).filter_by(name="ASAP-REQ").update({"deadline": "ASAP"})
        _req(db_session, test_user, name="FUTURE", customer="Acme")
        db_session.query(Requisition).filter_by(name="FUTURE").update({"deadline": "2099-01-01"})
        db_session.commit()
        intent = {"template": "open_requisitions", "params": {"past_deadline": True}}
        with patch("app.services.ask_avail_service.claude_structured", new_callable=AsyncMock, return_value=intent):
            out = await answer_question(db_session, test_user, "open reqs past their deadline")
        assert out["matched"] is True
        names = {r["name"] for r in out["rows"]}
        assert "PAST" in names
        assert "ASAP-REQ" not in names and "FUTURE" not in names

    def test_csv_past_deadline_does_not_500(self, client, db_session, test_user):
        """The CSV endpoint runs the template directly — a past_deadline export must not
        raise (the PG-invalid compare would 500 without the fix)."""
        _req(db_session, test_user, name="CSVPD")
        db_session.query(Requisition).filter_by(name="CSVPD").update({"deadline": "2000-01-01"})
        db_session.commit()
        resp = client.get(
            "/v2/partials/search/ask.csv",
            params={"template": "open_requisitions", "params": '{"past_deadline": true}'},
            headers=_HX,
        )
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")

    async def test_approval_cycle_time_gated_for_restricted_role(self, db_session, test_user, sales_user):
        """A restricted role must not see buy-plan approval timing for requisitions they
        don't own."""
        from app.constants import ApprovalGateType, ApprovalRequestStatus, ApprovalSubjectType
        from app.models.approvals import ApprovalRequest
        from app.models.buy_plan import BuyPlan
        from app.models.quotes import Quote
        from app.services.ask_avail_service import answer_question

        owner_req = _req(db_session, test_user, name="OWNER-APR")
        db_session.flush()
        q = Quote(requisition_id=owner_req.id, quote_number="Q-APR", status="sent", created_at=datetime.now(UTC))
        db_session.add(q)
        db_session.flush()
        bp = BuyPlan(
            requisition_id=owner_req.id,
            quote_id=q.id,
            status="active",
            so_status="approved",
            submitted_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(bp)
        db_session.flush()
        ar = ApprovalRequest(
            gate_type=ApprovalGateType.BUY_PLAN.value,
            status=ApprovalRequestStatus.REQUESTED.value,
            subject_type=ApprovalSubjectType.BUY_PLAN.value,
            subject_id=bp.id,
            requested_by_id=test_user.id,
            owner_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(ar)
        db_session.commit()
        intent = {"template": "approval_cycle_time", "params": {}}
        with patch("app.services.ask_avail_service.claude_structured", new_callable=AsyncMock, return_value=intent):
            out = await answer_question(db_session, sales_user, "approval cycle time")
        assert out["matched"] is True
        assert all(row["request_id"] != ar.id for row in out["rows"])  # foreign req's approval hidden

    async def test_effective_params_surface_hidden_defaults(self, db_session, test_user):
        """A template with a hidden default cutoff reports the EFFECTIVE params it
        actually ran with, not the AI's empty dict — so the UI can be honest."""
        from app.services.ask_avail_service import answer_question

        intent = {"template": "unanswered_quotes", "params": {}}
        with patch("app.services.ask_avail_service.claude_structured", new_callable=AsyncMock, return_value=intent):
            out = await answer_question(db_session, test_user, "unanswered quotes")
        assert out["params"].get("older_than_days") == 7  # the applied default is visible

    def test_bare_lookup_does_not_dispatch_ask_avail(self, client, db_session, test_user):
        """A short entity lookup (bare MPN / vendor name) must NOT spend a second Claude
        call on the report classifier — only question-shaped input dispatches."""
        entity = {"best_match": None, "groups": {}, "total_count": 0}
        with (
            patch("app.services.ask_avail_service.answer_question", new_callable=AsyncMock) as ask,
            patch("app.services.global_search_service.ai_search", new_callable=AsyncMock, return_value=entity),
        ):
            client.post("/v2/partials/search/ai", data={"q": "LM317T"}, headers=_HX)
        ask.assert_not_called()

    def test_question_shaped_query_dispatches_ask_avail(self, client, db_session, test_user):
        _req(db_session, test_user, name="QSHAPE-OPEN")
        db_session.commit()
        intent = {"template": "open_requisitions", "params": {}}
        with patch("app.services.ask_avail_service.claude_structured", new_callable=AsyncMock, return_value=intent):
            resp = client.post("/v2/partials/search/ai", data={"q": "open requisitions past deadline"}, headers=_HX)
        assert "QSHAPE-OPEN" in resp.text

    async def test_until_includes_the_named_final_day(self, db_session, test_user):
        from app.services.ask_avail_service import answer_question

        r = _req(db_session, test_user, name="ONLAST", customer="Acme")
        db_session.query(Requisition).filter_by(id=r.id).update(
            {"created_at": datetime(2026, 6, 15, 14, 0, tzinfo=UTC)}
        )
        db_session.commit()
        intent = {"template": "requisitions_by_customer", "params": {"customer": "Acme", "until": "2026-06-15"}}
        with patch("app.services.ask_avail_service.claude_structured", new_callable=AsyncMock, return_value=intent):
            out = await answer_question(db_session, test_user, "reqs for Acme")
        assert any(row["name"] == "ONLAST" for row in out["rows"])  # 14:00 on the until day is included

    async def test_like_wildcard_in_customer_is_escaped(self, db_session, test_user):
        """A '%' in a filter value matches literally, not as a wildcard."""
        from app.services.ask_avail_service import answer_question

        _req(db_session, test_user, name="R-REAL", customer="Acme Corp")
        _req(db_session, test_user, name="R-PCT", customer="100% Silicon")
        db_session.commit()
        intent = {"template": "open_requisitions", "params": {"customer": "100%"}}
        with patch("app.services.ask_avail_service.claude_structured", new_callable=AsyncMock, return_value=intent):
            out = await answer_question(db_session, test_user, "open reqs for 100%")
        names = {r["name"] for r in out["rows"]}
        assert "R-PCT" in names
        assert "R-REAL" not in names  # "%" did not act as a wildcard matching everything
