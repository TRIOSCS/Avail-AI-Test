"""Tests for lifecycle sweep job.

Tests the get_cards_for_lifecycle_check function which finds cards
that need lifecycle status verification (active or unknown status).

Depends on: app.jobs.lifecycle_jobs, app.models.MaterialCard
"""

from app.models import MaterialCard


def test_lifecycle_sweep_finds_active_cards(db_session):
    """Sweep should query cards marked active or with no lifecycle status."""
    from app.jobs.lifecycle_jobs import get_cards_for_lifecycle_check

    db_session.add(MaterialCard(normalized_mpn="a", display_mpn="A", lifecycle_status="active"))
    db_session.add(MaterialCard(normalized_mpn="b", display_mpn="B", lifecycle_status="obsolete"))
    db_session.add(MaterialCard(normalized_mpn="c", display_mpn="C", lifecycle_status=None))
    db_session.flush()

    cards = get_cards_for_lifecycle_check(db_session)
    mpns = {c.normalized_mpn for c in cards}
    assert "a" in mpns
    assert "c" in mpns
    assert "b" not in mpns


def test_lifecycle_sweep_excludes_deleted_cards(db_session):
    """Deleted cards should not be included in lifecycle checks."""
    from datetime import datetime, timezone

    from app.jobs.lifecycle_jobs import get_cards_for_lifecycle_check

    db_session.add(
        MaterialCard(
            normalized_mpn="deleted",
            display_mpn="DELETED",
            lifecycle_status="active",
            deleted_at=datetime.now(timezone.utc),
        )
    )
    db_session.add(MaterialCard(normalized_mpn="alive", display_mpn="ALIVE", lifecycle_status="active"))
    db_session.flush()

    cards = get_cards_for_lifecycle_check(db_session)
    mpns = {c.normalized_mpn for c in cards}
    assert "alive" in mpns
    assert "deleted" not in mpns


def test_lifecycle_sweep_respects_limit(db_session):
    """Sweep should respect the limit parameter."""
    from app.jobs.lifecycle_jobs import get_cards_for_lifecycle_check

    for i in range(10):
        db_session.add(MaterialCard(normalized_mpn=f"card{i}", display_mpn=f"CARD{i}", lifecycle_status="active"))
    db_session.flush()

    cards = get_cards_for_lifecycle_check(db_session, limit=3)
    assert len(cards) == 3
