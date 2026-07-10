"""Regression: the prospecting list's lazy-loaded stats panel must target ITSELF.

A `hx-trigger="load"` element with no `hx-target` inherits #main-content's
`hx-target="this"`, so its lazy load swaps the stats response INTO #main-content
and wipes the entire card grid (buckets show, cards vanish). curl-based verification
never caught this because curl does not execute htmx; this test does it at the HTML
level so the explicit hx-target can't silently regress.
"""

import os
import re

os.environ["TESTING"] = "1"

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.prospect_account import ProspectAccount


def _make(db: Session) -> ProspectAccount:
    p = ProspectAccount(
        name=f"Reg {uuid.uuid4().hex[:6]}",
        domain=f"reg-{uuid.uuid4().hex[:6]}.com",
        status="suggested",
        fit_score=70,
        readiness_score=50,
        discovery_source="manual",
        created_at=datetime.now(UTC),
    )
    db.add(p)
    db.commit()
    return p


def test_stats_panel_targets_itself_not_main_content(client, db_session):
    _make(db_session)
    html = client.get("/v2/partials/prospecting").text

    m = re.search(r'<div[^>]*id="prospect-stats"[^>]*>', html)
    assert m, "stats container (#prospect-stats) missing from the list partial"
    tag = m.group(0)
    assert 'hx-trigger="load"' in tag, "stats panel should lazy-load"
    # The crux: an explicit target so the lazy load fills the panel instead of
    # inheriting #main-content's hx-target='this' and replacing the whole grid.
    assert 'hx-target="#prospect-stats"' in tag, (
        "stats lazy-load is missing hx-target='#prospect-stats' — it would inherit "
        "#main-content's hx-target='this' and wipe the card grid on load"
    )


def test_no_prospecting_lazy_load_inherits_main_content_target():
    """Every hx-trigger='load' element across the prospecting templates carries an
    explicit hx-target (so none can inherit #main-content and hijack the page)."""
    import pathlib

    tdir = pathlib.Path(__file__).resolve().parent.parent / "app/templates/htmx/partials/prospecting"
    offenders = []
    for f in sorted(tdir.glob("*.html")):
        src = f.read_text()
        # Inspect each opening tag that contains a load trigger.
        for tag in re.findall(r"<[a-zA-Z][^>]*hx-trigger=\"[^\"]*load[^\"]*\"[^>]*>", src):
            if "hx-target=" not in tag:
                offenders.append(f"{f.name}: {tag[:120]}")
    assert not offenders, "lazy-load element(s) missing hx-target:\n" + "\n".join(offenders)
