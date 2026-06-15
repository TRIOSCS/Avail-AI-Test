"""Unit 3 — faceted results render a live result count + an aria-live announcement.

The count is match-framed: "N results [in <commodity>] [· matching "<q>"]" — the word
"results" (not "parts") makes clear it's how many matched the current search/filters.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import MaterialCard


def _card(db: Session, mpn: str) -> None:
    db.add(
        MaterialCard(
            normalized_mpn=mpn,
            display_mpn=mpn.upper(),
            category="dram",
            created_at=datetime.now(timezone.utc),
        )
    )


def test_faceted_renders_live_count_with_commodity(client, db_session: Session):
    _card(db_session, "m1")
    _card(db_session, "m2")
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted?commodity=dram")
    assert resp.status_code == 200
    # Count + display name + match-framed plural noun at the top of the results pane.
    assert "2" in resp.text
    assert "DRAM" in resp.text
    assert "results" in resp.text
    # Screen-reader announcement present.
    assert 'aria-live="polite"' in resp.text


def test_faceted_count_singular_no_commodity(client, db_session: Session):
    _card(db_session, "solo")
    db_session.commit()
    resp = client.get("/v2/partials/materials/faceted")
    assert resp.status_code == 200
    # Singular noun for a single result, no commodity name.
    assert "1" in resp.text
    assert "result" in resp.text
    # Singular, not pluralized, for exactly one match.
    assert "results" not in resp.text
