"""test_proactive_teams_push.py — Tests for the proactive→Teams push (Phase 1).

Covers app/services/proactive_teams_push.py (digest build, idempotent watermark,
status filtering) and the flag-gated job registration in app/jobs/offers_jobs.py.

Called by: pytest
Depends on: app.services.proactive_teams_push, app.jobs.offers_jobs, conftest fixtures
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.orm import Session

from app.models import Company, MaterialCard, Offer, ProactiveMatch, Requisition, User
from app.models.config import SystemConfig
from app.services.proactive_teams_push import _WATERMARK_KEY, push_new_matches_to_teams

_PUSH_PATH = "app.services.proactive_teams_push.post_teams_channel_card"


def _scaffold(db: Session) -> dict:
    """Minimal owner/company/offer needed to hang ProactiveMatch rows off of."""
    owner = User(email="s@trioscs.com", name="Rep", role="sales", azure_id="s-1", created_at=datetime.now(UTC))
    db.add(owner)
    db.flush()
    company = Company(name="Acme Corp", is_active=True, account_owner_id=owner.id)
    db.add(company)
    db.flush()
    card = MaterialCard(normalized_mpn="lm358n", display_mpn="LM358N")
    db.add(card)
    db.flush()
    req = Requisition(name="R", customer_site_id=None, status="archived", created_by=owner.id)
    db.add(req)
    db.flush()
    offer = Offer(
        requisition_id=req.id,
        material_card_id=card.id,
        vendor_name="Arrow",
        mpn="LM358N",
        unit_price=Decimal("0.42"),
        qty_available=5000,
        status="active",
    )
    db.add(offer)
    db.flush()
    return {"owner": owner, "company": company, "offer": offer}


def _add_match(db: Session, ctx: dict, mpn: str, score: int, status: str = "new") -> ProactiveMatch:
    match = ProactiveMatch(
        offer_id=ctx["offer"].id,
        salesperson_id=ctx["owner"].id,
        company_id=ctx["company"].id,
        mpn=mpn,
        match_score=score,
        status=status,
    )
    db.add(match)
    db.commit()
    return match


async def test_no_new_matches_returns_zero(db_session: Session):
    with patch(_PUSH_PATH, new=AsyncMock()) as mock_post:
        result = await push_new_matches_to_teams(db_session)
    assert result == {"pushed": 0}
    mock_post.assert_not_called()


async def test_pushes_new_matches_and_advances_watermark(db_session: Session):
    ctx = _scaffold(db_session)
    _add_match(db_session, ctx, "LM358N", 85)
    m2 = _add_match(db_session, ctx, "TL072CP", 60)

    with patch(_PUSH_PATH, new=AsyncMock()) as mock_post:
        result = await push_new_matches_to_teams(db_session)

    assert result == {"pushed": 2}
    mock_post.assert_awaited_once()
    card = mock_post.call_args.args[0]
    # Header reflects the count, FactSet carries the company name, action deep-links to AVAIL.
    assert "2 new proactive matches" in card["body"][0]["text"]
    facts = card["body"][1]["facts"]
    assert any("Acme Corp" in f["value"] for f in facts)
    assert card["actions"][0]["url"].endswith("/v2/proactive")
    # Watermark advanced to the highest match id seen.
    row = db_session.query(SystemConfig).filter(SystemConfig.key == _WATERMARK_KEY).first()
    assert row is not None and int(row.value) == m2.id


async def test_idempotent_second_run_pushes_nothing(db_session: Session):
    ctx = _scaffold(db_session)
    _add_match(db_session, ctx, "LM358N", 85)

    with patch(_PUSH_PATH, new=AsyncMock()) as mock_post:
        first = await push_new_matches_to_teams(db_session)
        second = await push_new_matches_to_teams(db_session)

    assert first == {"pushed": 1}
    assert second == {"pushed": 0}
    assert mock_post.await_count == 1


async def test_only_new_status_is_pushed(db_session: Session):
    ctx = _scaffold(db_session)
    _add_match(db_session, ctx, "SENT-MPN", 90, status="sent")
    _add_match(db_session, ctx, "NEW-MPN", 70, status="new")

    with patch(_PUSH_PATH, new=AsyncMock()) as mock_post:
        result = await push_new_matches_to_teams(db_session)

    assert result == {"pushed": 1}
    card = mock_post.call_args.args[0]
    titles = [f["title"] for f in card["body"][1]["facts"]]
    assert "NEW-MPN" in titles
    assert "SENT-MPN" not in titles


class TestPushJobRegistration:
    """The push job registers only when its flag is on."""

    def _register(self, push_enabled: bool) -> list[str]:
        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        mock_settings.proactive_matching_enabled = False
        mock_settings.proactive_scan_interval_hours = 4
        mock_settings.proactive_teams_push_enabled = push_enabled
        from app.jobs.offers_jobs import register_offers_jobs

        register_offers_jobs(mock_scheduler, mock_settings)
        return [c.kwargs.get("id") for c in mock_scheduler.add_job.call_args_list]

    def test_registered_when_flag_on(self):
        assert "proactive_teams_push" in self._register(push_enabled=True)

    def test_absent_when_flag_off(self):
        assert "proactive_teams_push" not in self._register(push_enabled=False)


async def test_watermark_advances_contiguously_no_skip(db_session: Session, monkeypatch):
    """With more NEW matches than the scan cap, the watermark advances by ID (a
    contiguous window), so lower-scored lower-id matches are NOT skipped forever — a
    second run picks up the remainder with no overlap.

    (Regression: the old score-ordered window jumped the watermark to the top-by-score
    max id.)
    """
    import app.services.proactive_teams_push as tp

    monkeypatch.setattr(tp, "_SCAN_CAP", 3)
    ctx = _scaffold(db_session)
    # Scores ASCEND with id, so top-by-score = the HIGHEST ids. The buggy window would
    # push ids {3,4,5} and jump the watermark to id5, orphaning ids {1,2}.
    ms = [_add_match(db_session, ctx, f"MPN{i}", score=10 * i) for i in range(1, 6)]

    with patch(_PUSH_PATH, new=AsyncMock()) as mock_post:
        first = await tp.push_new_matches_to_teams(db_session)
    assert first == {"pushed": 3}
    row = db_session.query(SystemConfig).filter(SystemConfig.key == _WATERMARK_KEY).first()
    assert int(row.value) == ms[2].id  # contiguous: 3rd id, NOT ms[4].id (top score)
    # Card is still score-sorted for display (highest score of the window first).
    facts = mock_post.call_args.args[0]["body"][1]["facts"]
    assert "MPN3" in facts[0]["title"]

    with patch(_PUSH_PATH, new=AsyncMock()) as mock_post2:
        second = await tp.push_new_matches_to_teams(db_session)
    assert second == {"pushed": 2}  # ids 4,5 — the remainder, none re-pushed
    assert int(db_session.query(SystemConfig).filter(SystemConfig.key == _WATERMARK_KEY).first().value) == ms[4].id
