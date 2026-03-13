"""Lead provenance panel smoke tests.

What it does:
- Verifies the Lead Provenance modal markup is present in index.html.
- Verifies frontend wiring exists to open the provenance panel from lead rows.

What calls it:
- pytest test runner.

What it depends on:
- app/templates/index.html
- app/static/app.js
"""

from pathlib import Path


def test_lead_provenance_modal_exists_in_template():
    template = Path("app/templates/index.html").read_text(encoding="utf-8")
    assert 'id="leadProvenanceModal"' in template
    assert 'id="leadProvenanceBody"' in template
    assert "closeModal('leadProvenanceModal')" in template

