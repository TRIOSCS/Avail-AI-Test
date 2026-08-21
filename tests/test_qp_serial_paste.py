"""test_qp_serial_paste.py — TDD tests for QP serial paste-to-rows (packing-list
import).

Covers the paste-a-packing-list flow on the Quality Plan Serial section:
  - parse_serial_paste service: AI extraction → sanitized row dicts (unknown keys
    dropped, values stripped/truncated, all-empty rows removed; None on AI failure;
    [] on empty paste without calling Claude).
  - POST /v2/qp/{id}/serial/parse: renders the preview partial (rows table, per-row
    checkboxes, hidden rows_json, confirm form) or an error/empty state; never writes.
  - POST /v2/qp/{id}/serial/bulk: inserts ONLY the checked rows via the same coercion
    as the single-row add (submitted_by = acting user), then re-renders the section.
    Rejects malformed rows_json (400) and oversized payloads (400); authz-gated by
    _load_qp_for_edit like every other QP mutation.
  - _section_serial.html renders the paste affordance wired to /serial/parse.

Called by: pytest (TESTING=1 PYTHONPATH=. pytest tests/test_qp_serial_paste.py -v)
Depends on: app.services.qp_serial_paste_service, app.routers.quality_plans,
            conftest (client, db_session, test_user, sales_user, test_customer_site).
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.buy_plan import BuyPlan
from app.models.quality_plan import QpSerialEntry, QualityPlan
from app.models.quotes import Quote
from app.models.sourcing import Requisition

_HX = {"HX-Request": "true"}

# ── Helpers ────────────────────────────────────────────────────────────────────


def _seed_qp(db: Session, owner_id: int, site_id: int) -> QualityPlan:
    """Create a requisition → quote → buy plan → QP chain owned by owner_id."""
    req = Requisition(
        name="QP-SER-001",
        status="open",
        customer_name="Acme Electronics",
        created_by=owner_id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    q = Quote(
        requisition_id=req.id,
        customer_site_id=site_id,
        quote_number="QT-QPS-001",
        status="sent",
        created_at=datetime.now(UTC),
    )
    db.add(q)
    db.flush()
    bp = BuyPlan(
        requisition_id=req.id,
        quote_id=q.id,
        status="draft",
        so_status="pending",
        sales_order_number="SO-QPS-1",
        submitted_by_id=owner_id,
    )
    db.add(bp)
    db.flush()
    qp = QualityPlan(buy_plan_id=bp.id, created_by_id=owner_id, status="draft", order_type="new")
    db.add(qp)
    db.commit()
    return qp


def _serial_rows(db: Session, qp_id: int) -> list[QpSerialEntry]:
    return list(
        db.execute(select(QpSerialEntry).where(QpSerialEntry.qp_id == qp_id).order_by(QpSerialEntry.id)).scalars()
    )


_THREE_ROWS = [
    {
        "purchase_order": "PO-1001",
        "part_number": "ST4000NM000A",
        "serial_number": "ZC11AAAA",
        "seagate_sn": "SG-1",
        "tso": "TSO-77",
        "customer_po": "CPO-5",
    },
    {
        "purchase_order": "PO-1001",
        "part_number": "ST4000NM000A",
        "serial_number": "ZC11BBBB",
        "seagate_sn": None,
        "tso": None,
        "customer_po": None,
    },
    {
        "purchase_order": "PO-1002",
        "part_number": "ST8000NM017B",
        "serial_number": "ZC22CCCC",
        "seagate_sn": "SG-3",
        "tso": "TSO-78",
        "customer_po": "CPO-6",
    },
]


@pytest.fixture()
def restricted_client(db_session: Session, sales_user: User):
    """TestClient authenticated as a restricted-role (sales) user who owns nothing."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: sales_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)


# ── Service: parse_serial_paste ────────────────────────────────────────────────


class TestParseSerialPasteService:
    async def test_maps_and_sanitizes_rows(self):
        from app.services.qp_serial_paste_service import parse_serial_paste

        ai_result = {
            "rows": [
                {  # padded values get stripped; unknown keys dropped
                    "purchase_order": "  PO-1001 ",
                    "part_number": "ST4000NM000A",
                    "serial_number": " ZC11AAAA ",
                    "seagate_sn": "",
                    "tso": None,
                    "customer_po": "CPO-5",
                    "bogus_key": "ignored",
                },
                {"serial_number": "X" * 300},  # over-length truncated to 255
                {"purchase_order": "", "part_number": " ", "serial_number": None},  # all-empty dropped
            ]
        }
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=ai_result):
            rows = await parse_serial_paste("PO-1001 ST4000NM000A ZC11AAAA ...")

        assert rows == [
            {
                "purchase_order": "PO-1001",
                "part_number": "ST4000NM000A",
                "serial_number": "ZC11AAAA",
                "seagate_sn": None,
                "tso": None,
                "customer_po": "CPO-5",
            },
            {
                "purchase_order": None,
                "part_number": None,
                "serial_number": "X" * 255,
                "seagate_sn": None,
                "tso": None,
                "customer_po": None,
            },
        ]

    async def test_empty_text_returns_empty_without_ai_call(self):
        from app.services.qp_serial_paste_service import parse_serial_paste

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as mock:
            assert await parse_serial_paste("   \n  ") == []
        mock.assert_not_called()

    async def test_ai_failure_returns_none(self):
        from app.services.qp_serial_paste_service import parse_serial_paste

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=None):
            assert await parse_serial_paste("some packing list") is None


# ── Route: POST /v2/qp/{id}/serial/parse ───────────────────────────────────────


class TestSerialParseRoute:
    def test_parse_renders_preview_with_confirm_form(self, client, db_session, test_user, test_customer_site):
        qp = _seed_qp(db_session, test_user.id, test_customer_site.id)
        with patch("app.routers.quality_plans.parse_serial_paste", new_callable=AsyncMock, return_value=_THREE_ROWS):
            resp = client.post(f"/v2/qp/{qp.id}/serial/parse", data={"pasted_text": "raw packing list"}, headers=_HX)
        assert resp.status_code == 200
        body = resp.text
        # All three rows previewed with per-row checked checkboxes.
        assert "ZC11AAAA" in body and "ZC11BBBB" in body and "ZC22CCCC" in body
        assert body.count('name="include"') == 3
        assert body.count("checked") >= 3
        # Confirm form carries the rows and posts to the bulk endpoint; nothing written yet.
        assert f"/v2/qp/{qp.id}/serial/bulk" in body
        assert 'name="rows_json"' in body
        assert _serial_rows(db_session, qp.id) == []

    def test_parse_ai_failure_shows_error_not_confirm(self, client, db_session, test_user, test_customer_site):
        qp = _seed_qp(db_session, test_user.id, test_customer_site.id)
        with patch("app.routers.quality_plans.parse_serial_paste", new_callable=AsyncMock, return_value=None):
            resp = client.post(f"/v2/qp/{qp.id}/serial/parse", data={"pasted_text": "raw"}, headers=_HX)
        assert resp.status_code == 200
        assert "could not parse" in resp.text.lower()
        assert "serial/bulk" not in resp.text

    def test_parse_empty_paste_shows_message_without_ai(self, client, db_session, test_user, test_customer_site):
        qp = _seed_qp(db_session, test_user.id, test_customer_site.id)
        with patch("app.routers.quality_plans.parse_serial_paste", new_callable=AsyncMock, return_value=[]) as mock:
            resp = client.post(f"/v2/qp/{qp.id}/serial/parse", data={"pasted_text": ""}, headers=_HX)
        assert resp.status_code == 200
        assert "nothing to parse" in resp.text.lower()
        mock.assert_not_called()

    def test_parse_unauthenticated_401(self, unauthenticated_client, db_session, test_user, test_customer_site):
        qp = _seed_qp(db_session, test_user.id, test_customer_site.id)
        resp = unauthenticated_client.post(f"/v2/qp/{qp.id}/serial/parse", data={"pasted_text": "x"}, headers=_HX)
        assert resp.status_code == 401


# ── Route: POST /v2/qp/{id}/serial/bulk ────────────────────────────────────────


class TestSerialBulkRoute:
    def test_bulk_inserts_only_checked_rows(self, client, db_session, test_user, test_customer_site):
        qp = _seed_qp(db_session, test_user.id, test_customer_site.id)
        resp = client.post(
            f"/v2/qp/{qp.id}/serial/bulk",
            data={"rows_json": json.dumps(_THREE_ROWS), "include": ["0", "2"]},
            headers=_HX,
        )
        assert resp.status_code == 200
        rows = _serial_rows(db_session, qp.id)
        assert [r.serial_number for r in rows] == ["ZC11AAAA", "ZC22CCCC"]
        first = rows[0]
        assert first.purchase_order == "PO-1001"
        assert first.part_number == "ST4000NM000A"
        assert first.seagate_sn == "SG-1"
        assert first.tso == "TSO-77"
        assert first.customer_po == "CPO-5"
        assert first.submitted_by_id == test_user.id
        # Response is the refreshed section partial showing the new rows.
        assert "ZC11AAAA" in resp.text and "ZC22CCCC" in resp.text
        assert 'id="qp-section-serial"' in resp.text

    def test_bulk_ignores_out_of_range_and_junk_indices(self, client, db_session, test_user, test_customer_site):
        qp = _seed_qp(db_session, test_user.id, test_customer_site.id)
        # "²" is isdigit()-true but int()-invalid; "9"*5000 trips int()'s max_str_digits —
        # both must be silently skipped like any other junk index, never a 500.
        resp = client.post(
            f"/v2/qp/{qp.id}/serial/bulk",
            data={"rows_json": json.dumps(_THREE_ROWS), "include": ["1", "99", "-1", "abc", "²", "9" * 5000]},
            headers=_HX,
        )
        assert resp.status_code == 200
        rows = _serial_rows(db_session, qp.id)
        assert [r.serial_number for r in rows] == ["ZC11BBBB"]

    def test_bulk_deeply_nested_rows_json_400(self, client, db_session, test_user, test_customer_site):
        """A recursion-bomb payload gets the intended 400, not an unhandled 500."""
        qp = _seed_qp(db_session, test_user.id, test_customer_site.id)
        resp = client.post(
            f"/v2/qp/{qp.id}/serial/bulk",
            data={"rows_json": "[" * 20000 + "]" * 20000, "include": ["0"]},
            headers=_HX,
        )
        assert resp.status_code == 400
        assert _serial_rows(db_session, qp.id) == []

    def test_bulk_nothing_checked_inserts_nothing(self, client, db_session, test_user, test_customer_site):
        qp = _seed_qp(db_session, test_user.id, test_customer_site.id)
        resp = client.post(f"/v2/qp/{qp.id}/serial/bulk", data={"rows_json": json.dumps(_THREE_ROWS)}, headers=_HX)
        assert resp.status_code == 200
        assert _serial_rows(db_session, qp.id) == []

    def test_bulk_malformed_json_400(self, client, db_session, test_user, test_customer_site):
        qp = _seed_qp(db_session, test_user.id, test_customer_site.id)
        resp = client.post(
            f"/v2/qp/{qp.id}/serial/bulk", data={"rows_json": "{not json", "include": ["0"]}, headers=_HX
        )
        assert resp.status_code == 400
        assert _serial_rows(db_session, qp.id) == []

    def test_bulk_oversized_payload_400(self, client, db_session, test_user, test_customer_site):
        qp = _seed_qp(db_session, test_user.id, test_customer_site.id)
        big = [{"serial_number": f"SN-{i}"} for i in range(301)]
        resp = client.post(
            f"/v2/qp/{qp.id}/serial/bulk", data={"rows_json": json.dumps(big), "include": ["0"]}, headers=_HX
        )
        assert resp.status_code == 400
        assert _serial_rows(db_session, qp.id) == []

    def test_bulk_restricted_non_owner_404(self, restricted_client, db_session, test_user, test_customer_site):
        """The new write path enforces the same ownership rule as every QP mutation."""
        qp = _seed_qp(db_session, test_user.id, test_customer_site.id)
        resp = restricted_client.post(
            f"/v2/qp/{qp.id}/serial/bulk",
            data={"rows_json": json.dumps(_THREE_ROWS), "include": ["0"]},
            headers=_HX,
        )
        assert resp.status_code == 404
        assert _serial_rows(db_session, qp.id) == []


# ── Template: paste affordance in the Serial section ───────────────────────────


def test_serial_section_renders_paste_affordance(client, db_session, test_user, test_customer_site):
    qp = _seed_qp(db_session, test_user.id, test_customer_site.id)
    resp = client.get(f"/v2/qp/{qp.id}", headers=_HX)
    assert resp.status_code == 200
    body = resp.text
    assert f"/v2/qp/{qp.id}/serial/parse" in body
    assert 'name="pasted_text"' in body


def test_row_actions_target_entries_div_so_preview_survives(client, db_session, test_user, test_customer_site):
    """Row Delete and the single-add form swap only #qp-serial-entries (via hx-select),
    so an open paste panel / unconfirmed preview outside that div is not destroyed."""
    qp = _seed_qp(db_session, test_user.id, test_customer_site.id)
    client.post(f"/v2/qp/{qp.id}/serial", data={"serial_number": "ZKEEP1"}, headers=_HX)
    resp = client.get(f"/v2/qp/{qp.id}", headers=_HX)
    body = resp.text
    assert 'id="qp-serial-entries"' in body
    # The delete button and add form both narrow their swap to the entries div.
    assert body.count('hx-target="#qp-serial-entries"') == 2
    assert body.count('hx-select="#qp-serial-entries"') == 2
    # The paste preview mount lives OUTSIDE the entries div (after it in the section).
    assert body.index('id="qp-serial-entries"') < body.index('id="qp-serial-paste-preview"')


class TestSerialChipOob:
    def test_bulk_response_carries_oob_chip_with_new_count(self, client, db_session, test_user, test_customer_site):
        qp = _seed_qp(db_session, test_user.id, test_customer_site.id)
        resp = client.post(
            f"/v2/qp/{qp.id}/serial/bulk",
            data={"rows_json": json.dumps(_THREE_ROWS), "include": ["0", "2"]},
            headers=_HX,
        )
        assert resp.status_code == 200
        assert 'id="qp-serial-chip"' in resp.text
        assert 'hx-swap-oob="true"' in resp.text
        assert "2 entries" in resp.text

    def test_detail_render_has_single_chip_and_no_oob_twin(self, client, db_session, test_user, test_customer_site):
        """The full QP detail (which {% include %}s the section) must not render a
        duplicate OOB chip span — the guard keeps it to the refresh responses."""
        qp = _seed_qp(db_session, test_user.id, test_customer_site.id)
        resp = client.get(f"/v2/qp/{qp.id}", headers=_HX)
        assert resp.text.count('id="qp-serial-chip"') == 1
        # (the page's <title> legitimately uses hx-swap-oob — only the chip twin is guarded)
        assert 'id="qp-serial-chip" hx-swap-oob' not in resp.text
