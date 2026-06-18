import os

os.environ["TESTING"] = "1"
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models.intelligence import MaterialCard
from app.services import datasheet_capture as dc


@pytest.fixture(autouse=True)
def _session(db_session):
    with patch("app.services.datasheet_capture.SessionLocal", lambda: db_session):
        yield db_session


async def test_capture_stores_verified_connector_datasheet(_session, test_user):
    card = MaterialCard(normalized_mpn="17p9905", display_mpn="17P9905", datasheet_url="https://ti/17P9905.pdf")
    _session.add(card)
    _session.commit()
    # Capture id before the call — db.close() expires all ORM state including test_user.
    user_id = test_user.id
    with (
        patch("app.services.datasheet_capture._load_user", return_value=test_user),
        patch("app.services.datasheet_capture.download_pdf", AsyncMock(return_value=b"%PDF-1.4 data")),
        patch(
            "app.services.datasheet_capture.upload_bytes_to_onedrive",
            AsyncMock(return_value={"onedrive_item_id": "01", "onedrive_url": "https://od/x", "size_bytes": 12}),
        ),
    ):
        await dc.capture_datasheet("17P9905", user_id)
    card = _session.query(MaterialCard).filter_by(normalized_mpn="17p9905").first()
    assert len(card.datasheets) == 1
    assert card.datasheets[0].source == "connector"
    assert card.datasheets[0].verified is True
    assert card.datasheets[0].uploaded_by_id == user_id
    assert card.datasheet_captured_at is not None


async def test_capture_skips_within_cooldown(_session):
    card = MaterialCard(
        normalized_mpn="ne555",
        display_mpn="NE555",
        datasheet_searched_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    _session.add(card)
    _session.commit()
    with patch("app.services.datasheet_capture.find_datasheet_url", AsyncMock()) as find:
        await dc.capture_datasheet("NE555", 1)
        find.assert_not_called()


async def test_capture_web_hit_rejected_when_mpn_absent(_session, test_user):
    card = MaterialCard(normalized_mpn="17p9905", display_mpn="17P9905")  # no connector url
    _session.add(card)
    _session.commit()
    with (
        patch("app.services.datasheet_capture._load_user", return_value=test_user),
        patch(
            "app.services.datasheet_capture.find_datasheet_url", AsyncMock(return_value=("https://x/wrong.pdf", "web"))
        ),
        patch("app.services.datasheet_capture.download_pdf", AsyncMock(return_value=b"%PDF wrong")),
        patch("app.services.datasheet_capture.pdf_contains_mpn", return_value=False),
        patch("app.services.datasheet_capture.upload_bytes_to_onedrive", AsyncMock()) as up,
    ):
        await dc.capture_datasheet("17P9905", test_user.id)
        up.assert_not_called()
    card = _session.query(MaterialCard).filter_by(normalized_mpn="17p9905").first()
    assert card.datasheets == []
    assert card.datasheet_searched_at is not None  # negative cache stamped
