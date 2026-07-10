"""tests/test_rfq_attachments_s6.py — S6 datasheet attachment tests.

Covers:
- collect_rfq_attachments: resolves MaterialCardDatasheet rows, fetches bytes,
  base64-encodes, returns attachment list + status list.
- collect_rfq_attachments: oversized set is trimmed (largest-first drop) and logged.
- collect_rfq_attachments: missing bytes (fetch_datasheet_bytes returns None) → status=missing.
- collect_rfq_attachments: fetch error (exception) → status=fetch_error.
- send_batch_rfq with attachments=[...]: correct @odata.type/contentBytes on EVERY vendor.
- send_batch_rfq with no attachments: payload byte-identical to today (regression guard).
- send_batch_rfq with oversized set: collect_rfq_attachments handles the cap correctly.

Called by: pytest
Depends on: app.services.rfq_attachments, app.email_service
"""

import base64
import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import RfqAttachmentStatus
from app.models import (
    MaterialCard,
    Requirement,
    Requisition,
    User,
)
from app.models.intelligence import MaterialCardDatasheet
from tests.conftest import engine  # noqa: F401

# ── Helpers ──────────────────────────────────────────────────────────


def _make_card_with_datasheet(
    db: Session,
    mpn: str,
    file_name: str,
    library_item_id: str = "item-001",
    library_drive_id: str = "drive-abc",
    content_type: str = "application/pdf",
    size_bytes: int = 1024,
) -> tuple[MaterialCard, MaterialCardDatasheet]:
    mc = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn.upper(),
        manufacturer="TestMfr",
        created_at=datetime.now(UTC),
    )
    db.add(mc)
    db.flush()
    ds = MaterialCardDatasheet(
        material_card_id=mc.id,
        file_name=file_name,
        library_item_id=library_item_id,
        library_drive_id=library_drive_id,
        content_type=content_type,
        size_bytes=size_bytes,
        verified=True,
    )
    db.add(ds)
    db.flush()
    return mc, ds


def _make_requirement_for_card(db: Session, user: User, card: MaterialCard) -> Requirement:
    req = Requisition(
        owner_id=user.id,
        title="Test Req",
        status="open",
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn=card.display_mpn,
        manufacturer="TestMfr",
        target_qty=10,
        material_card_id=card.id,
    )
    db.add(r)
    db.flush()
    return r


# ── collect_rfq_attachments ──────────────────────────────────────────


class TestCollectRfqAttachments:
    @pytest.mark.asyncio
    async def test_returns_attachment_for_selected_datasheet(self, db_session: Session, test_user: User):
        """Selected datasheet resolves to RfqAttachment with correct name/b64."""
        mc, ds = _make_card_with_datasheet(db_session, "MPN001", "MPN001.pdf")
        fake_bytes = b"PDF content here"

        with patch(
            "app.services.rfq_attachments.fetch_datasheet_bytes",
            new_callable=AsyncMock,
            return_value=fake_bytes,
        ):
            from app.services.rfq_attachments import collect_rfq_attachments

            attachments, statuses = await collect_rfq_attachments(
                db=db_session,
                material_card_ids=[mc.id],
                selected_ids=[ds.id],
            )

        assert len(attachments) == 1
        att = attachments[0]
        assert att.name == "MPN001.pdf"
        assert att.content_type == "application/pdf"
        assert att.content_bytes_b64 == base64.b64encode(fake_bytes).decode()

    @pytest.mark.asyncio
    async def test_status_attached_for_fetched_datasheet(self, db_session: Session, test_user: User):
        """Status list includes 'attached' entry for a successfully fetched
        datasheet."""
        mc, ds = _make_card_with_datasheet(db_session, "MPN002", "MPN002.pdf")
        fake_bytes = b"PDF bytes"

        with patch(
            "app.services.rfq_attachments.fetch_datasheet_bytes",
            new_callable=AsyncMock,
            return_value=fake_bytes,
        ):
            from app.services.rfq_attachments import collect_rfq_attachments

            _, statuses = await collect_rfq_attachments(
                db=db_session,
                material_card_ids=[mc.id],
                selected_ids=[ds.id],
            )

        assert len(statuses) == 1
        assert statuses[0]["datasheet_id"] == ds.id
        assert statuses[0]["status"] == RfqAttachmentStatus.ATTACHED

    @pytest.mark.asyncio
    async def test_status_missing_when_bytes_none(self, db_session: Session, test_user: User):
        """fetch_datasheet_bytes returning None marks status as 'missing'."""
        mc, ds = _make_card_with_datasheet(db_session, "MPN003", "MPN003.pdf")

        with patch(
            "app.services.rfq_attachments.fetch_datasheet_bytes",
            new_callable=AsyncMock,
            return_value=None,
        ):
            from app.services.rfq_attachments import collect_rfq_attachments

            attachments, statuses = await collect_rfq_attachments(
                db=db_session,
                material_card_ids=[mc.id],
                selected_ids=[ds.id],
            )

        assert len(attachments) == 0
        assert statuses[0]["status"] == RfqAttachmentStatus.MISSING

    @pytest.mark.asyncio
    async def test_status_fetch_error_on_exception(self, db_session: Session, test_user: User):
        """Exception from fetch_datasheet_bytes marks status as 'fetch_error'."""
        mc, ds = _make_card_with_datasheet(db_session, "MPN004", "MPN004.pdf")

        with patch(
            "app.services.rfq_attachments.fetch_datasheet_bytes",
            new_callable=AsyncMock,
            side_effect=RuntimeError("network error"),
        ):
            from app.services.rfq_attachments import collect_rfq_attachments

            attachments, statuses = await collect_rfq_attachments(
                db=db_session,
                material_card_ids=[mc.id],
                selected_ids=[ds.id],
            )

        assert len(attachments) == 0
        assert statuses[0]["status"] == RfqAttachmentStatus.FETCH_ERROR

    @pytest.mark.asyncio
    async def test_oversized_largest_dropped_first(self, db_session: Session, test_user: User):
        """When combined size exceeds ~3MB, largest attachments are dropped first."""
        # Create 2 datasheets: large (2.5MB) + small (1MB) — combined 3.5MB exceeds cap
        mc1, ds1 = _make_card_with_datasheet(
            db_session,
            "MPN_BIG",
            "big.pdf",
            library_item_id="item-big",
            size_bytes=2_500_000,
        )
        mc2, ds2 = _make_card_with_datasheet(
            db_session,
            "MPN_SMALL",
            "small.pdf",
            library_item_id="item-small",
            size_bytes=1_000_000,
        )
        big_bytes = b"B" * 2_500_000
        small_bytes = b"S" * 1_000_000

        async def _fetch(drive_id, item_id):
            return big_bytes if item_id == "item-big" else small_bytes

        with patch("app.services.rfq_attachments.fetch_datasheet_bytes", side_effect=_fetch):
            from app.services.rfq_attachments import collect_rfq_attachments

            attachments, statuses = await collect_rfq_attachments(
                db=db_session,
                material_card_ids=[mc1.id, mc2.id],
                selected_ids=[ds1.id, ds2.id],
            )

        # Only small one should survive (big dropped first)
        assert len(attachments) == 1
        assert attachments[0].name == "small.pdf"
        # Status list should have the oversized entry
        oversized = [s for s in statuses if s["status"] == RfqAttachmentStatus.OVERSIZED]
        assert len(oversized) == 1
        assert oversized[0]["datasheet_id"] == ds1.id

    @pytest.mark.asyncio
    async def test_empty_selected_ids_returns_empty(self, db_session: Session, test_user: User):
        """Empty selected_ids returns empty lists immediately."""
        mc, _ = _make_card_with_datasheet(db_session, "MPN005", "MPN005.pdf")

        from app.services.rfq_attachments import collect_rfq_attachments

        attachments, statuses = await collect_rfq_attachments(
            db=db_session,
            material_card_ids=[mc.id],
            selected_ids=[],
        )

        assert attachments == []
        assert statuses == []

    @pytest.mark.asyncio
    async def test_status_missing_for_no_library_ids(self, db_session: Session, test_user: User):
        """Datasheet with null library_item_id/drive_id is marked 'missing'."""
        mc = MaterialCard(
            normalized_mpn="mpn_nolib",
            display_mpn="MPN_NOLIB",
            manufacturer="TestMfr",
            created_at=datetime.now(UTC),
        )
        db_session.add(mc)
        db_session.flush()
        ds = MaterialCardDatasheet(
            material_card_id=mc.id,
            file_name="nolib.pdf",
            library_item_id=None,  # not in library yet
            library_drive_id=None,
            verified=False,
        )
        db_session.add(ds)
        db_session.flush()

        from app.services.rfq_attachments import collect_rfq_attachments

        attachments, statuses = await collect_rfq_attachments(
            db=db_session,
            material_card_ids=[mc.id],
            selected_ids=[ds.id],
        )

        assert len(attachments) == 0
        assert statuses[0]["status"] == RfqAttachmentStatus.MISSING


# ── send_batch_rfq with attachments ─────────────────────────────────


class TestSendBatchRfqAttachments:
    """Tests for the attachments param on send_batch_rfq."""

    def _make_vendor_group(self, vendor_name: str, email: str) -> dict:
        return {
            "vendor_name": vendor_name,
            "vendor_email": email,
            "parts": [{"mpn": "ABC123", "qty": 5}],
            "subject": "RFQ — 1 part",
            "body": "Please quote.",
        }

    @pytest.mark.asyncio
    async def test_attachments_included_in_each_vendor_payload(self, db_session: Session, test_user: User):
        """send_batch_rfq with attachments puts @odata.type + contentBytes on EACH
        vendor."""
        from app.services.rfq_attachments import RfqAttachment

        fake_bytes = b"PDF data"
        att = RfqAttachment(
            name="sheet.pdf",
            content_type="application/pdf",
            content_bytes_b64=base64.b64encode(fake_bytes).decode(),
        )

        captured_payloads = []

        async def _mock_post_json(path, payload):
            captured_payloads.append(payload)
            return {"id": "msg-001"}

        mock_gc = MagicMock()
        mock_gc.post_json = _mock_post_json

        # Two vendors
        groups = [
            self._make_vendor_group("Vendor A", "a@example.com"),
            self._make_vendor_group("Vendor B", "b@example.com"),
        ]

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            from app.email_service import send_batch_rfq

            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=None,
                vendor_groups=groups,
                attachments=[att],
            )

        assert len(captured_payloads) == 2
        for payload in captured_payloads:
            msg_attachments = payload["message"].get("attachments", [])
            assert len(msg_attachments) == 1
            a = msg_attachments[0]
            assert a["@odata.type"] == "#microsoft.graph.fileAttachment"
            assert a["name"] == "sheet.pdf"
            assert a["contentType"] == "application/pdf"
            assert a["contentBytes"] == base64.b64encode(fake_bytes).decode()

    @pytest.mark.asyncio
    async def test_no_attachments_payload_identical_to_today(self, db_session: Session, test_user: User):
        """send_batch_rfq with no attachments → no 'attachments' key in payload."""
        captured_payloads = []

        async def _mock_post_json(path, payload):
            captured_payloads.append(payload)
            return {"id": "msg-002"}

        mock_gc = MagicMock()
        mock_gc.post_json = _mock_post_json

        groups = [self._make_vendor_group("Vendor C", "c@example.com")]

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            from app.email_service import send_batch_rfq

            await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=None,
                vendor_groups=groups,
                # No attachments param
            )

        assert len(captured_payloads) == 1
        assert "attachments" not in captured_payloads[0]["message"]

    @pytest.mark.asyncio
    async def test_none_attachments_payload_identical_to_today(self, db_session: Session, test_user: User):
        """send_batch_rfq with attachments=None → no 'attachments' key in payload."""
        captured_payloads = []

        async def _mock_post_json(path, payload):
            captured_payloads.append(payload)
            return {"id": "msg-003"}

        mock_gc = MagicMock()
        mock_gc.post_json = _mock_post_json

        groups = [self._make_vendor_group("Vendor D", "d@example.com")]

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            from app.email_service import send_batch_rfq

            await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=None,
                vendor_groups=groups,
                attachments=None,
            )

        assert len(captured_payloads) == 1
        assert "attachments" not in captured_payloads[0]["message"]

    @pytest.mark.asyncio
    async def test_empty_attachments_list_no_key_in_payload(self, db_session: Session, test_user: User):
        """send_batch_rfq with attachments=[] → no 'attachments' key in payload."""
        captured_payloads = []

        async def _mock_post_json(path, payload):
            captured_payloads.append(payload)
            return {"id": "msg-004"}

        mock_gc = MagicMock()
        mock_gc.post_json = _mock_post_json

        groups = [self._make_vendor_group("Vendor E", "e@example.com")]

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            from app.email_service import send_batch_rfq

            await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=None,
                vendor_groups=groups,
                attachments=[],
            )

        assert len(captured_payloads) == 1
        assert "attachments" not in captured_payloads[0]["message"]

    @pytest.mark.asyncio
    async def test_multiple_attachments_all_included(self, db_session: Session, test_user: User):
        """Multiple attachments all appear in each vendor payload."""
        from app.services.rfq_attachments import RfqAttachment

        atts = [
            RfqAttachment(
                name=f"file{i}.pdf",
                content_type="application/pdf",
                content_bytes_b64=base64.b64encode(f"content{i}".encode()).decode(),
            )
            for i in range(3)
        ]

        captured_payloads = []

        async def _mock_post_json(path, payload):
            captured_payloads.append(payload)
            return {"id": f"msg-multi-{len(captured_payloads)}"}

        mock_gc = MagicMock()
        mock_gc.post_json = _mock_post_json

        groups = [self._make_vendor_group("Vendor F", "f@example.com")]

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            from app.email_service import send_batch_rfq

            await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=None,
                vendor_groups=groups,
                attachments=atts,
            )

        assert len(captured_payloads) == 1
        msg_attachments = captured_payloads[0]["message"]["attachments"]
        assert len(msg_attachments) == 3
        names = {a["name"] for a in msg_attachments}
        assert names == {"file0.pdf", "file1.pdf", "file2.pdf"}


# ── Router-level header test ─────────────────────────────────────────


class TestSendInquiryDatasheetDroppedHeader:
    """Verify the router sets X-RFQ-Datasheets-Dropped when collect_rfq_attachments
    returns oversized statuses."""

    def test_datasheets_dropped_header_set_on_oversized(self, client, db_session: Session, test_user: User):
        """Endpoint sets X-RFQ-Datasheets-Dropped == '1' when collect returns one
        oversized status entry."""
        from app.models.sourcing import Requirement, Requisition
        from app.services.rfq_attachments import RfqAttachment

        # Seed a minimal requisition + requirement so the endpoint can resolve them.
        req = Requisition(name="DS Test Req", status="open", customer_name="DS Corp")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="DS-MPN-001",
            manufacturer="TestMfr",
            target_qty=5,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.commit()

        # collect_rfq_attachments returns one attachment (kept) + one oversized status.
        kept_att = RfqAttachment(
            name="kept.pdf",
            content_type="application/pdf",
            content_bytes_b64="ZmFrZQ==",
        )
        ds_statuses = [
            {"datasheet_id": 99, "file_name": "big.pdf", "status": RfqAttachmentStatus.OVERSIZED},
        ]

        async def _fake_collect(**_kwargs):
            return [kept_att], ds_statuses

        async def _fake_send(**_kwargs):
            return [{"vendor_name": "Acme", "status": "sent"}]

        with (
            patch("app.services.rfq_attachments.collect_rfq_attachments", side_effect=_fake_collect),
            patch("app.email_service.send_batch_rfq", side_effect=_fake_send),
        ):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": ["Acme"],
                    "email_body": "Please quote.",
                    "datasheet_ids": "99",
                },
            )

        assert resp.status_code == 200
        assert resp.headers["X-RFQ-Datasheets-Dropped"] == "1"

    def test_datasheets_dropped_header_set_on_fetch_error(self, client, db_session: Session, test_user: User):
        """Endpoint sets X-RFQ-Datasheets-Dropped == '1' when collect returns one
        fetch_error status entry (not just oversized)."""
        from app.models.sourcing import Requirement, Requisition

        req = Requisition(name="DS FE Req", status="open", customer_name="DS Corp")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="DS-FE-001",
            manufacturer="TestMfr",
            target_qty=5,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.commit()

        # collect returns no attachments + one fetch_error status
        ds_statuses = [
            {"datasheet_id": 101, "file_name": "err.pdf", "status": RfqAttachmentStatus.FETCH_ERROR},
        ]

        async def _fake_collect(**_kwargs):
            return [], ds_statuses

        async def _fake_send(**_kwargs):
            return [{"vendor_name": "Acme", "status": "sent"}]

        with (
            patch("app.services.rfq_attachments.collect_rfq_attachments", side_effect=_fake_collect),
            patch("app.email_service.send_batch_rfq", side_effect=_fake_send),
        ):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": ["Acme"],
                    "email_body": "Please quote.",
                    "datasheet_ids": "101",
                },
            )

        assert resp.status_code == 200
        assert resp.headers["X-RFQ-Datasheets-Dropped"] == "1"

    def test_datasheets_dropped_header_set_on_missing(self, client, db_session: Session, test_user: User):
        """Endpoint sets X-RFQ-Datasheets-Dropped == '1' when collect returns one
        missing status entry."""
        from app.models.sourcing import Requirement, Requisition

        req = Requisition(name="DS Miss Req", status="open", customer_name="DS Corp")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="DS-MISS-001",
            manufacturer="TestMfr",
            target_qty=5,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.commit()

        ds_statuses = [
            {"datasheet_id": 102, "file_name": "missing.pdf", "status": RfqAttachmentStatus.MISSING},
        ]

        async def _fake_collect(**_kwargs):
            return [], ds_statuses

        async def _fake_send(**_kwargs):
            return [{"vendor_name": "Acme", "status": "sent"}]

        with (
            patch("app.services.rfq_attachments.collect_rfq_attachments", side_effect=_fake_collect),
            patch("app.email_service.send_batch_rfq", side_effect=_fake_send),
        ):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": ["Acme"],
                    "email_body": "Please quote.",
                    "datasheet_ids": "102",
                },
            )

        assert resp.status_code == 200
        assert resp.headers["X-RFQ-Datasheets-Dropped"] == "1"

    def test_datasheets_dropped_header_full_count_on_collect_exception(
        self, client, db_session: Session, test_user: User
    ):
        """When collect_rfq_attachments raises, X-RFQ-Datasheets-Dropped == total
        selected count (degrade-to-send-without + honest reporting)."""
        from app.models.sourcing import Requirement, Requisition

        req = Requisition(name="DS Exc Req", status="open", customer_name="DS Corp")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="DS-EXC-001",
            manufacturer="TestMfr",
            target_qty=5,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.commit()

        async def _fake_collect(**_kwargs):
            raise RuntimeError("graph unavailable")

        async def _fake_send(**_kwargs):
            return [{"vendor_name": "Acme", "status": "sent"}]

        # 2 datasheet ids selected — both should be reported as dropped
        with (
            patch("app.services.rfq_attachments.collect_rfq_attachments", side_effect=_fake_collect),
            patch("app.email_service.send_batch_rfq", side_effect=_fake_send),
        ):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": ["Acme"],
                    "email_body": "Please quote.",
                    "datasheet_ids": ["103", "104"],
                },
            )

        assert resp.status_code == 200
        assert resp.headers["X-RFQ-Datasheets-Dropped"] == "2"
