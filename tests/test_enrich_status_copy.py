"""SP1 status copy: the running fragment advertises contacts + firmographics."""

from pathlib import Path

_TPL = Path("app/templates/htmx/partials/prospecting/enrich_status.html").read_text()


def test_running_copy_mentions_contacts_and_firmographics():
    assert "Enriching… contacts + firmographics" in _TPL
    assert "SAM.gov + news" not in _TPL  # old copy removed
